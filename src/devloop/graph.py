"""A Python import graph built from the AST, with ``patch(...)`` edges.

Product modules are nodes named by dotted module path; files under the tests
root use their importable name (``tests.ui.support``), and nodes that are test
files are also registered in ``tests`` so a reverse walk can return them.

The graph is cached by content hash; a parse failure is recorded in
``unparsed`` and the caller fails open.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from devloop import cache as caching
from devloop.config import Config

GRAPH_KEY = "import-graph"
GRAPH_VERSION = 3


def _python_files(bases: list[Path]) -> list[Path]:
    files: list[Path] = []
    for base in bases:
        if base.is_file() and base.suffix == ".py":
            files.append(base)
        elif base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def module_name(path: Path, roots: list[Path]) -> str | None:
    """The dotted module name of a file under one of ``roots``."""
    resolved = path.resolve()
    for base in roots:
        try:
            relative = resolved.relative_to(base.resolve())
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return None


def _package_of(module: str) -> str:
    return module.rsplit(".", 1)[0] if "." in module else ""


def _resolve_relative(module: str, level: int, target: str | None, known: dict[str, str]) -> str:
    package = module if Path(known.get(module, "")).name == "__init__.py" else _package_of(module)
    for _ in range(max(0, level - 1)):
        package = _package_of(package)
    if target:
        return f"{package}.{target}" if package else target
    return package


def _patch_target(call: ast.Call) -> str | None:
    """The dotted string a ``patch("a.b")`` or ``import_module("a.b")`` uses."""
    name = ""
    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        name = call.func.attr
    if name not in {"patch", "import_module"} or not call.args:
        return None
    value = call.args[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _longest_known(target: str, known: dict[str, str]) -> str | None:
    parts = target.split(".")
    for end in range(len(parts), 1, -1):
        candidate = ".".join(parts[:end])
        if candidate in known:
            return candidate
    return None


def extract_imports(tree: ast.Module, module: str, known: dict[str, str]) -> set[str]:
    """Directly imported nodes of one parsed file."""
    found: set[str] = set()
    for node in ast.walk(tree):
        found |= _node_imports(node, module, known)
    return found


def _node_imports(node: ast.AST, module: str, known: dict[str, str]) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return _from_imports(node, module, known)
    if isinstance(node, ast.Call):
        target = _patch_target(node)
        resolved = _longest_known(target, known) if target else None
        return {resolved} if resolved else set()
    return set()


def _from_imports(node: ast.ImportFrom, module: str, known: dict[str, str]) -> set[str]:
    base = (
        _resolve_relative(module, node.level, node.module, known)
        if node.level
        else (node.module or "")
    )
    if not base:
        return set()
    found = {base}
    found.update(f"{base}.{alias.name}" for alias in node.names if f"{base}.{alias.name}" in known)
    return found


@dataclass
class ImportGraph:
    """Forward and reverse edges over product modules and test files."""

    imports: dict[str, set[str]] = field(default_factory=dict)
    reverse: dict[str, set[str]] = field(default_factory=dict)
    tests: dict[str, str] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)
    unparsed: set[str] = field(default_factory=set)

    def dependents(self, seeds: set[str]) -> set[str]:
        """Every test node reachable by walking imports backwards from ``seeds``."""
        seen: set[str] = set()
        found: set[str] = set()
        queue = list(seeds)
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in self.tests:
                found.add(node)
            queue.extend(self.reverse.get(node, ()))
        return found


@dataclass
class Layout:
    """Where the project's modules and tests live."""

    root: Path
    source_roots: list[Path]
    import_roots: list[Path]
    tests_root: Path

    @classmethod
    def of(cls, cfg: Config) -> "Layout":
        source_roots = [cfg.root / root for root in cfg.source_roots]
        return cls(
            root=cfg.root,
            source_roots=source_roots,
            import_roots=[root.parent for root in source_roots],
            tests_root=cfg.root / cfg.tests_root,
        )


def _names(layout: Layout) -> tuple[dict[str, str], dict[str, str]]:
    """Map product modules and test-tree modules to repo-relative paths."""
    modules: dict[str, str] = {}
    for path in _python_files(layout.source_roots):
        name = module_name(path, layout.import_roots)
        if name:
            modules[name] = _relative(layout.root, path)
    test_modules: dict[str, str] = {}
    for path in _python_files([layout.tests_root]):
        name = module_name(path, [layout.root])
        if name:
            test_modules[name] = _relative(layout.root, path)
    return modules, test_modules


def source_module(cfg: Config, path: Path) -> str | None:
    """The importable name of a product file (the package root's parent is the root)."""
    roots = [(cfg.root / root).parent for root in cfg.source_roots]
    return module_name(path, roots)


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _build(cfg: Config) -> ImportGraph:
    layout = Layout.of(cfg)
    modules, test_modules = _names(layout)
    known = {**modules, **test_modules}
    graph = ImportGraph(modules={**modules, **test_modules})
    graph.tests = {
        name: path
        for name, path in test_modules.items()
        if Path(path).name.startswith("test_") or Path(path).name.endswith("_test.py")
    }

    for name, relative in known.items():
        path = cfg.root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            graph.unparsed.add(relative)
            continue
        graph.imports[name] = extract_imports(tree, name, known)
    _rebuild_reverse(graph)
    return graph


def _rebuild_reverse(graph: ImportGraph) -> None:
    graph.reverse = {}
    for node, targets in graph.imports.items():
        for target in targets:
            graph.reverse.setdefault(target, set()).add(node)


def _fingerprint(cfg: Config, layout: Layout) -> dict:
    inputs = _python_files(layout.source_roots) + _python_files([layout.tests_root])
    return {
        "version": GRAPH_VERSION,
        "files": caching.hash_paths(cfg.root, inputs),
    }


def build(cfg: Config) -> ImportGraph:
    """Load the graph from the cache when no input changed, else rebuild."""
    layout = Layout.of(cfg)
    fingerprint = _fingerprint(cfg, layout)
    cache = caching.Cache(cfg.cache_dir / "import-graph.json")
    cached = cache.get(GRAPH_KEY)
    if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
        graph = ImportGraph(
            imports={key: set(value) for key, value in cached.get("imports", {}).items()},
            tests=cached.get("tests", {}),
            modules=cached.get("modules", {}),
            unparsed=set(cached.get("unparsed", [])),
        )
        _rebuild_reverse(graph)
        return graph

    graph = _build(cfg)
    cache.put(
        GRAPH_KEY,
        {
            "fingerprint": fingerprint,
            "imports": {key: sorted(value) for key, value in graph.imports.items()},
            "tests": graph.tests,
            "modules": graph.modules,
            "unparsed": sorted(graph.unparsed),
        },
    )
    cache.save()
    return graph
