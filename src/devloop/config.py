"""Read project settings from ``[tool.devloop]`` in ``pyproject.toml``."""

from __future__ import annotations

import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "tests_root": "tests",
    "cache_dir": ".devloop",
    "runner": "",
    "quality": "",
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


def _table(root: Path) -> dict:
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return data.get("tool", {}).get("devloop", {})


def find_root(start: Path | None = None) -> Path:
    """Find the configured project from the working directory or a child."""
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and _table(candidate):
            return candidate
    raise ValueError("no [tool.devloop] table found above the current directory")


def load(root: Path | None = None) -> Config:
    """Resolve settings for the configured project."""
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
    )
