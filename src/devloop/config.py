"""Project settings read from ``[tool.devloop]`` in ``pyproject.toml``.

Everything project-specific lives in that one table, so this package works in
any repository unchanged. The selector, the quality gate, the policy gate and
the test runner are commands: a project plugs in its own implementations and
this package stays free of project knowledge.

.. code-block:: toml

    [tool.devloop]
    source_roots = ["src/my_package"]
    tests_root = "tests"
    cache_dir = ".devloop"
    runner = "tools/run_tests.py"
    quality = "tools/check_quality.py"
    policy = "tools/check_package_policy.py"
    selector = ["{python}", "-m", "devloop", "select-tests"]
    shared = ["pyproject.toml", "poetry.lock"]
    always = ["tests/test_contract.py"]
    max_fraction = 0.6
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "tests_root": "tests",
    "cache_dir": ".devloop",
    "runner": "",
    "quality": "",
    "policy": "",
    "selector": ["{python}", "-m", "devloop", "select-tests"],
    "shared": ["pyproject.toml"],
    "always": [],
    "max_fraction": 0.6,
    "baseline": "devloop-baseline.json",
}

CHECK_PATHS = ("none", "changed", "all")


@dataclass(frozen=True)
class CheckSpec:
    """One extra command in the pipeline, from ``[[tool.devloop.check]]``."""

    name: str
    command: tuple[str, ...]
    paths: str = "none"


@dataclass(frozen=True)
class Config:
    """The resolved project settings for one run."""

    root: Path
    python: str
    source_roots: tuple[str, ...]
    tests_root: str
    cache_dir: Path
    runner: Path | None
    quality: Path | None
    selector: tuple[str, ...]
    shared: tuple[str, ...]
    always: tuple[str, ...]
    max_fraction: float
    policy: Path | None = None
    baseline: Path | None = None
    env: tuple[tuple[str, str], ...] = ()
    mypy_scope: tuple[str, ...] = ()
    mypy_changed_only: bool = False
    checks: tuple[CheckSpec, ...] = ()

    @property
    def environment(self) -> dict[str, str]:
        """The extra variables every step subprocess receives."""
        return dict(self.env)

    def relative(self, path: Path) -> str:
        """Return a repository-relative POSIX path."""
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def resolve_command(self, template: tuple[str, ...]) -> list[str]:
        """Expand ``{python}`` and find a bare executable by the interpreter."""
        if not template:
            return []
        command = [self.python if part == "{python}" else part for part in template]
        command[0] = self._executable(command[0])
        return command

    def _executable(self, name: str) -> str:
        if Path(name).is_file():
            return name
        beside = Path(self.python).parent / name
        for candidate in (beside, beside.with_name(name + ".exe")):
            if candidate.is_file():
                return str(candidate)
        return shutil.which(name) or name


def _table(root: Path) -> dict[str, Any]:
    """The ``[tool.devloop]`` table of one project, or an empty dict."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    tool = data.get("tool", {})
    table = tool.get("devloop", {}) if isinstance(tool, dict) else {}
    return table if isinstance(table, dict) else {}


def find_root(start: Path | None = None) -> Path:
    """Find the configured project from the working directory or a child."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if _table(candidate):
            return candidate
    raise ValueError("no [tool.devloop] table found above the current directory")


def _baseline_path(root: Path, raw: object) -> Path | None:
    """The baseline file, or ``None`` when the key is an empty string."""
    if not isinstance(raw, str):
        raise ValueError('[tool.devloop] baseline must be a path or ""')
    text = raw.strip()
    return (root / text).resolve() if text else None


def _environment(root: Path, raw: object) -> tuple[tuple[str, str], ...]:
    """``[tool.devloop.env]`` with ``{root}`` expanded, sorted for stability."""
    if not isinstance(raw, dict):
        raise ValueError("[tool.devloop.env] must be a table of strings")
    values: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ValueError(f"[tool.devloop.env] {name} must be a string")
        values[str(name)] = value.replace("{root}", root.as_posix())
    return tuple(sorted(values.items()))


def _mypy_settings(root: Path, raw: object) -> tuple[tuple[str, ...], bool]:
    """``[tool.devloop.mypy]``: the scope directories and the changed-only flag."""
    if not isinstance(raw, dict):
        raise ValueError("[tool.devloop.mypy] must be a table")
    entries = raw.get("scope", [])
    if not isinstance(entries, list) or not all(
        isinstance(item, str) and item.strip() for item in entries
    ):
        raise ValueError("[tool.devloop.mypy] scope must be a list of paths")
    scope: list[str] = []
    for entry in entries:
        clean = entry.strip().replace("\\", "/").rstrip("/")
        path = Path(clean)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"[tool.devloop.mypy] scope must stay inside the project: {entry}")
        if not (root / clean).exists():
            raise ValueError(f"[tool.devloop.mypy] scope entry does not exist: {entry}")
        scope.append(clean)
    changed_only = raw.get("changed_only", False)
    if not isinstance(changed_only, bool):
        raise ValueError("[tool.devloop.mypy] changed_only must be a boolean")
    return tuple(scope), changed_only


def _checks(raw: object) -> tuple[CheckSpec, ...]:
    """``[[tool.devloop.check]]``: extra commands run before the tests."""
    if not isinstance(raw, list):
        raise ValueError("[[tool.devloop.check]] must be an array of tables")
    specs: list[CheckSpec] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("[[tool.devloop.check]] must be an array of tables")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("[[tool.devloop.check]] each entry needs a name")
        name = name.strip()
        if name in names:
            raise ValueError(f"[[tool.devloop.check]] duplicate name: {name}")
        command = item.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ValueError(f"[[tool.devloop.check]] {name}: command must be a list of strings")
        paths = item.get("paths", "none")
        if paths not in CHECK_PATHS:
            raise ValueError(
                f"[[tool.devloop.check]] {name}: paths must be one of {', '.join(CHECK_PATHS)}"
            )
        names.add(name)
        specs.append(CheckSpec(name, tuple(command), paths))
    return tuple(specs)


def load(root: Path | None = None) -> Config:
    """Resolve the settings of the project at ``root`` (or the nearest one)."""
    root = (root or find_root()).resolve()
    table = _table(root)
    if not table:
        raise ValueError(f"no [tool.devloop] table in {root / 'pyproject.toml'}")
    settings = {**DEFAULTS, **table}
    roots = settings.get("source_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(item, str) for item in roots):
        raise ValueError("[tool.devloop] source_roots must be a non-empty list of paths")
    fraction = float(settings["max_fraction"])
    if not 0 < fraction <= 1:
        raise ValueError("[tool.devloop] max_fraction must be greater than 0 and at most 1")
    scope, changed_only = _mypy_settings(root, table.get("mypy", {}))
    return Config(
        root=root,
        python=sys.executable,
        source_roots=tuple(roots),
        tests_root=str(settings["tests_root"]),
        cache_dir=root / str(settings["cache_dir"]),
        runner=root / str(settings["runner"]) if settings["runner"] else None,
        quality=root / str(settings["quality"]) if settings["quality"] else None,
        selector=tuple(settings["selector"]),
        shared=tuple(settings["shared"]),
        always=tuple(settings["always"]),
        max_fraction=fraction,
        policy=root / str(settings["policy"]) if settings["policy"] else None,
        baseline=_baseline_path(root, settings["baseline"]),
        env=_environment(root, table.get("env", {})),
        mypy_scope=scope,
        mypy_changed_only=changed_only,
        checks=_checks(table.get("check", [])),
    )
