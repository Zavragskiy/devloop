"""Affected-test selection through a configurable selector command.

The selector prints affected test paths, one per line. Any tool that does that
works (``impacted-tests``, ``pytest-selective affected``, a project script).
The wrapper adds the always-run guard tests and fails open to the full suite
when the selector fails, returns nothing, or selects most of the suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from devloop import process
from devloop.config import Config
from devloop.gitinfo import Change


@dataclass(frozen=True)
class Selection:
    """The test files a run should execute and why."""

    tests: tuple[str, ...]
    reason: str
    fail_open: bool = False

    @property
    def empty(self) -> bool:
        return not self.tests


def all_tests(cfg: Config) -> list[str]:
    """Every discovered test file, repo-relative and sorted."""
    root = cfg.root / cfg.tests_root
    if not root.is_dir():
        return []
    paths = set(root.rglob("test_*.py")) | set(root.rglob("*_test.py"))
    return sorted(cfg.relative(path) for path in paths)


def _prefix(relative: str, root: str) -> bool:
    root = root.rstrip("/")
    return relative == root or relative.startswith(root + "/")


def _is_test(cfg: Config, relative: str) -> bool:
    path = Path(relative)
    return (
        (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and path.suffix == ".py"
        and _prefix(relative, cfg.tests_root)
    )


def _is_source_python(cfg: Config, relative: str) -> bool:
    return relative.endswith(".py") and any(_prefix(relative, root) for root in cfg.source_roots)


def _under_source(cfg: Config, relative: str) -> bool:
    return any(_prefix(relative, root) for root in cfg.source_roots)


def _shared(cfg: Config, relative: str) -> bool:
    return any(_prefix(relative, entry) for entry in cfg.shared)


def _normalise(cfg: Config, line: str) -> str | None:
    """One selector output line as a repo-relative test path, or ``None``."""
    text = line.strip().replace("\\", "/")
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = cfg.root / path
    try:
        relative = cfg.relative(path)
    except ValueError:
        return None
    if not (cfg.root / relative).is_file():
        return None
    return relative if _is_test(cfg, relative) else None


def parse(cfg: Config, output: str) -> list[str]:
    """Test paths named in the selector output, de-duplicated in order."""
    found: list[str] = []
    for line in output.splitlines():
        relative = _normalise(cfg, line)
        if relative is not None and relative not in found:
            found.append(relative)
    return found


def _fail_open(cfg: Config, reason: str) -> Selection:
    return Selection(tuple(all_tests(cfg)), reason, fail_open=True)


def select(
    cfg: Config,
    changes: list[Change],
    base: str | None = None,
    full: bool = False,
) -> Selection:
    """Choose the test files for ``changes``, erring towards more tests."""
    every = all_tests(cfg)
    if not every:
        return Selection((), "no test files discovered")
    if full:
        return Selection(tuple(every), "full run requested")

    early = _early_reason(cfg, changes)
    if early:
        return _fail_open(cfg, early)
    if not _relevant(cfg, changes):
        return Selection((), "no Python source or test changed")

    command = cfg.resolve_command(cfg.selector)
    if not command:
        return _fail_open(cfg, "no selector configured")
    if base:
        command += ["--base", base]
    return _run_selector(cfg, every, command)


def _early_reason(cfg: Config, changes: list[Change]) -> str | None:
    deleted = next(
        (
            change.path
            for change in changes
            if change.deleted
            and (
                _under_source(cfg, change.path)
                or _prefix(change.path, cfg.tests_root)
                or change.path == "conftest.py"
            )
        ),
        None,
    )
    if deleted:
        return f"source or test deleted: {deleted}"
    changed = [change.path for change in changes]
    conftest = _first_conftest(cfg, changed)
    if conftest:
        return f"conftest changed: {conftest}"
    if _first_shared(cfg, changed):
        return "a shared input changed"
    if _first_asset(cfg, changed):
        return "a non-Python source asset changed"
    return None


def _first_conftest(cfg: Config, changed: list[str]) -> str | None:
    return next(
        (path for path in changed if Path(path).name == "conftest.py"),
        None,
    )


def _first_shared(cfg: Config, changed: list[str]) -> str | None:
    return next((path for path in changed if _shared(cfg, path)), None)


def _first_asset(cfg: Config, changed: list[str]) -> str | None:
    return next(
        (path for path in changed if _under_source(cfg, path) and not path.endswith(".py")),
        None,
    )


def _relevant(cfg: Config, changes: list[Change]) -> bool:
    return any(
        _is_source_python(cfg, change.path) or _is_test(cfg, change.path)
        for change in changes
        if not change.deleted
    )


def _run_selector(cfg: Config, every: list[str], command: list[str]) -> Selection:
    try:
        result = process.run(command, cwd=cfg.root)
    except OSError as error:
        return _fail_open(cfg, f"selector could not start: {error}")
    if result.returncode != 0:
        return _fail_open(cfg, f"selector failed with exit {result.returncode}")
    if any(_normalise(cfg, line) is None for line in result.stdout.splitlines() if line.strip()):
        return _fail_open(cfg, "selector returned an invalid test path")
    selected = parse(cfg, result.stdout)
    if not selected:
        return _fail_open(cfg, "selector selected no tests")
    selected = _with_always(cfg, selected)
    if len(selected) >= cfg.max_fraction * len(every):
        return _fail_open(cfg, f"selection is broad ({len(selected)} of {len(every)} tests)")
    return Selection(tuple(sorted(selected)), f"selector selected {len(selected)} of {len(every)}")


def _with_always(cfg: Config, selected: list[str]) -> list[str]:
    for relative in cfg.always:
        if relative not in selected and (cfg.root / relative).is_file():
            selected.append(relative)
    return selected
