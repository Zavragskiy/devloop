"""The ``check``, ``impact`` and ``changed`` commands."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

from devloop import config, gitinfo, impact, selector
from devloop.config import Config


def _python(cfg: Config, *arguments: str) -> list[str]:
    return [cfg.python, *arguments]


def _all_python(cfg: Config) -> list[str]:
    files: list[str] = []
    bases = [cfg.root / root for root in cfg.source_roots] + [cfg.root / cfg.tests_root]
    for base in bases:
        if base.is_file() and base.suffix == ".py":
            files.append(cfg.relative(base))
        elif base.is_dir():
            files.extend(cfg.relative(path) for path in sorted(base.rglob("*.py")))
    return files


def _is_source(cfg: Config, relative: str) -> bool:
    return any(
        relative == root.rstrip("/") or relative.startswith(root.rstrip("/") + "/")
        for root in cfg.source_roots
    )


class Steps:
    """Run verification steps in order, timing them and stopping on failure."""

    def __init__(self, cfg: Config, keep_going: bool):
        self.cfg = cfg
        self.keep_going = keep_going
        self.failures: list[str] = []
        self.seconds: float = 0.0

    @property
    def stopped(self) -> bool:
        return bool(self.failures) and not self.keep_going

    def run(self, label: str, command: list[str] | None) -> None:
        if command is None or self.stopped:
            return
        print(f"[verify] {label} ...")
        started = time.monotonic()
        code = subprocess.run(command, cwd=self.cfg.root, text=True).returncode
        elapsed = time.monotonic() - started
        self.seconds += elapsed
        print(f"[verify] {label}: {'ok' if code == 0 else 'FAILED'} ({elapsed:.1f}s)")
        if code != 0:
            self.failures.append(label)


def run_check(args: argparse.Namespace) -> int:
    cfg = config.load()
    changes = gitinfo.collect(cfg.root, args.base)
    files = _changed_python(cfg, changes)
    scope = " (full)" if args.full else ""
    print(f"[verify] {_changed_count(changes)} changed, {len(files)} Python file(s){scope}")

    steps = Steps(cfg, args.keep_going)
    _static_steps(cfg, steps, files, args.full)
    _test_step(cfg, steps, changes, args)
    return _finish(steps)


def _changed_count(changes: list[gitinfo.Change]) -> int:
    return sum(1 for change in changes if not change.deleted)


def _changed_python(cfg: Config, changes: list[gitinfo.Change]) -> list[str]:
    return [
        change.path
        for change in changes
        if not change.deleted and change.path.endswith(".py") and (cfg.root / change.path).is_file()
    ]


def _static_steps(cfg: Config, steps: Steps, files: list[str], full: bool) -> None:
    checked = _all_python(cfg) if full else files
    if checked:
        steps.run("ruff", _python(cfg, "-m", "ruff", "check", *checked))
        steps.run("format", _python(cfg, "-m", "ruff", "format", "--check", *checked))
    if cfg.quality and checked:
        steps.run("quality", _python(cfg, str(cfg.quality), *checked))
    if full or any(_is_source(cfg, path) for path in files):
        steps.run("mypy", _python(cfg, "-m", "mypy"))


def _test_step(
    cfg: Config, steps: Steps, changes: list[gitinfo.Change], args: argparse.Namespace
) -> None:
    selection = impact.select(cfg, changes, base=args.base, full=args.full)
    print(f"[verify] tests: {selection.reason}")
    if args.explain:
        for path in selection.tests:
            print(f"  {path}")
    if args.no_tests or selection.empty:
        print("[verify] tests: skipped")
        return
    if cfg.runner:
        command = _python(cfg, str(cfg.runner), "--files", *selection.tests)
        if args.jobs:
            command += ["--jobs", str(args.jobs)]
    else:
        command = _python(cfg, "-m", "pytest", "-q", *selection.tests)
    steps.run("tests", command)


def _finish(steps: Steps) -> int:
    print(f"[verify] {'FAILED' if steps.failures else 'ok'} in {steps.seconds:.1f}s")
    if steps.failures:
        print(f"[verify] failed: {', '.join(steps.failures)}")
        return 1
    return 0


def run_impact(args: argparse.Namespace) -> int:
    cfg = config.load()
    selection = impact.select(
        cfg, gitinfo.collect(cfg.root, args.base), base=args.base, full=args.full
    )
    print(selection.reason)
    for path in selection.tests:
        print(path)
    return 0


def run_changed(args: argparse.Namespace) -> int:
    cfg = config.load()
    for change in gitinfo.collect(cfg.root, args.base):
        print(f"{change.status} {change.path}")
    return 0


def run_select_tests(args: argparse.Namespace) -> int:
    cfg = config.load()
    for path in selector.select(cfg, gitinfo.collect(cfg.root, args.base)):
        print(path)
    return 0


def build_parser(default: str = "check") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devloop", description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default=default,
        choices=("check", "impact", "changed", "select-tests"),
    )
    parser.add_argument("--full", action="store_true", help="check everything, not only changes")
    parser.add_argument("--base", default=None, help="compare against this git ref")
    parser.add_argument("--jobs", type=int, default=0, help="test shards to run in parallel")
    parser.add_argument("--no-tests", action="store_true", help="skip the test step")
    parser.add_argument(
        "--keep-going", action="store_true", help="do not stop on the first failure"
    )
    parser.add_argument("--explain", action="store_true", help="print the selected test files")
    return parser


def main(argv: list[str] | None = None, default: str = "check") -> int:
    args = build_parser(default).parse_args(argv)
    try:
        if args.command == "impact":
            return run_impact(args)
        if args.command == "changed":
            return run_changed(args)
        if args.command == "select-tests":
            return run_select_tests(args)
        return run_check(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"devloop: {error}", file=sys.stderr)
        return 2
