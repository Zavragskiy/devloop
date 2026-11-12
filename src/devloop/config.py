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
}


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
    )
