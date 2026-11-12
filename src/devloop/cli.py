"""The ``check``, ``impact``, ``changed`` and ``select-tests`` commands.

``check`` is the pipeline: the static gates, then the tests affected by the
change. ``--full`` checks every tracked file and runs the whole suite; a plain
run is the edit loop. ``impact`` prints the selected files, ``changed`` lists
the git entries the selection sees, and ``select-tests`` is the built-in
selector command (one test path per line) that ``check`` runs by default.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass

from devloop import config, gitinfo, impact, report, selector, verdict
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

    def run(self, label: str, command: list[str] | None) -> bool:
        """Run one step; True when it passed or was skipped."""
        if command is None or self.stopped:
            return True
        print(f"[verify] {label} ...")
        started = time.monotonic()
        code = subprocess.run(command, cwd=self.cfg.root, text=True).returncode
        elapsed = time.monotonic() - started
        self.seconds += elapsed
        print(f"[verify] {label}: {'ok' if code == 0 else 'FAILED'} ({elapsed:.1f}s)")
        if code != 0:
            self.failures.append(label)
        return code == 0


def run_check(args: argparse.Namespace) -> int:
    cfg = config.load()
    changes = gitinfo.collect(cfg.root, args.base)
    files = _changed_python(cfg, changes)
    scope = " (full)" if args.full else ""
    print(f"[verify] {_changed_count(changes)} changed, {len(files)} Python file(s){scope}")

    key = _cache_key(cfg, args)
    if key is not None and verdict.recent(cfg, key):
        print("[verify] this tree already passed the full gate; nothing to re-check")
        return 0
    steps = Steps(cfg, args.keep_going)
    _static_steps(cfg, steps, files, args.full)
    _test_step(cfg, steps, changes, args)
    code = _finish(steps)
    if code == 0 and key is not None:
        verdict.remember(cfg, key)
    return code


def _cache_key(cfg: Config, args: argparse.Namespace) -> str | None:
    """The tree key when this run may be replayed from the cache, else ``None``."""
    cacheable = args.full and not args.base and not args.no_cache and not args.no_tests
    if not cacheable or _explicit_selection(args):
        return None
    return verdict.tree_key(cfg)


def _changed_count(changes: list[gitinfo.Change]) -> int:
    return sum(1 for change in changes if not change.deleted)


def _changed_python(cfg: Config, changes: list[gitinfo.Change]) -> list[str]:
    return [
        change.path
        for change in changes
        if not change.deleted and change.path.endswith(".py") and (cfg.root / change.path).is_file()
    ]


def _static_steps(cfg: Config, steps: Steps, files: list[str], full: bool) -> None:
    gated = _all_python(cfg) if full else files
    if gated:
        steps.run("ruff", _python(cfg, "-m", "ruff", "check", *gated))
        steps.run("format", _python(cfg, "-m", "ruff", "format", "--check", *gated))
    if cfg.quality is not None and gated:
        steps.run("quality", _python(cfg, str(cfg.quality), *gated))
    if cfg.policy is not None:
        steps.run("policy", _python(cfg, str(cfg.policy)))
    if full or any(_is_source(cfg, path) for path in files):
        steps.run("mypy", _python(cfg, "-m", "mypy"))


def _test_step(
    cfg: Config, steps: Steps, changes: list[gitinfo.Change], args: argparse.Namespace
) -> None:
    command = _test_command(cfg, changes, args)
    if command is None:
        print("[verify] tests: skipped")
        return
    if not steps.run("tests", command):
        report.test_failures(cfg)


def _explicit_selection(args: argparse.Namespace) -> bool:
    """True when the caller named the tests, so selection is not run."""
    return bool(args.files or args.only or args.last_failed)


@dataclass(frozen=True)
class Request:
    """What the test step should run, stated without the runner's flags."""

    files: tuple[str, ...] = ()
    only: tuple[str, ...] = ()
    last_failed: bool = False


def _test_command(
    cfg: Config, changes: list[gitinfo.Change], args: argparse.Namespace
) -> list[str] | None:
    """The test command for this run, or ``None`` when nothing should run."""
    if args.no_tests:
        return None
    if _explicit_selection(args):
        print("[verify] tests: explicit selection")
        if args.files and args.explain:
            _indented(args.files)
        request = Request(tuple(args.files), tuple(args.only), args.last_failed)
        return _command(cfg, args, request)
    selection = impact.select(cfg, changes, base=args.base, full=args.full)
    print(f"[verify] tests: {selection.reason}")
    if args.explain:
        _indented(selection.tests)
    if selection.empty:
        return None
    return _command(cfg, args, Request(files=selection.tests))


def _command(cfg: Config, args: argparse.Namespace, request: Request) -> list[str]:
    """The configured runner, or direct pytest when the project has none."""
    if cfg.runner is None:
        return _pytest_command(cfg, request)
    return _runner_command(cfg, args, request)


def _runner_command(cfg: Config, args: argparse.Namespace, request: Request) -> list[str]:
    """Forward the request to the project's runner as ``--files``/``--only`` flags."""
    command = _python(cfg, str(cfg.runner))
    if request.files:
        command += ["--files", *request.files]
    for token in request.only:
        command += ["--only", token]
    if request.last_failed:
        command.append("--last-failed")
    if args.jobs:
        command += ["--jobs", str(args.jobs)]
    return command


def _pytest_command(cfg: Config, request: Request) -> list[str]:
    """Direct pytest: files are positional, ``--only`` filters paths, ``--lf`` replays.

    ``--jobs`` means nothing without a sharding runner, so it is ignored here.
    """
    command = _python(cfg, "-m", "pytest", "-q")
    if request.last_failed:
        command.append("--lf")
    if request.only:
        matched = _only_tests(cfg, request.only, request.files)
        if not matched:
            raise ValueError("no test file matches: " + ", ".join(request.only))
        return command + matched
    return command + list(request.files)


def _only_tests(cfg: Config, tokens: tuple[str, ...], files: tuple[str, ...]) -> list[str]:
    """Test files whose path contains one of ``tokens``.

    ``files`` narrows the search; without it every discovered test is a
    candidate. Callers reject an empty result rather than run the whole suite.
    """
    pool = files or tuple(impact.all_tests(cfg))
    return [path for path in pool if any(token in path for token in tokens)]


def _indented(paths: list[str] | tuple[str, ...]) -> None:
    for path in paths:
        print(f"  {path}")


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


EPILOG = """\
Two modes: the default checks the changed files and the tests they affect (the
edit loop), --full checks every tracked file and runs the whole suite (the
commit and stage gate). Reach for --full before a commit or at the end of a
stage, not after every edit. A green --full run is remembered for the exact
tree it gated, so repeating it on an unchanged tree answers in about a second;
--no-cache ignores that record.
"""

DESCRIPTION = """\
Run the verification pipeline configured in [tool.devloop]: the static gates,
then the tests affected by the change.
"""


def build_parser(default: str = "check", prog: str = "devloop") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=default,
        choices=("check", "impact", "changed", "select-tests"),
        help="check runs the pipeline (default); impact prints the selected test files; "
        "changed lists the git changes the selection sees; select-tests prints the test "
        "files the built-in selector picks",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="check everything, not only changes: the commit/stage gate "
        "(the default changed mode is the edit loop)",
    )
    parser.add_argument("--base", default=None, help="compare against this git ref")
    parser.add_argument("--jobs", type=int, default=0, help="test shards to run in parallel")
    parser.add_argument("--no-tests", action="store_true", help="skip the test step")
    parser.add_argument(
        "--files",
        nargs="+",
        default=[],
        metavar="PATH",
        help="run only these test files (implies an explicit selection)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TOKEN",
        help="run only test files whose path contains TOKEN",
    )
    parser.add_argument(
        "--last-failed",
        action="store_true",
        help="replay the tests that failed in the last recorded run",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore a recorded green verdict for this tree",
    )
    parser.add_argument(
        "--keep-going", action="store_true", help="do not stop on the first failure"
    )
    parser.add_argument("--explain", action="store_true", help="print the selected test files")
    return parser


def main(argv: list[str] | None = None, default: str = "check", prog: str = "devloop") -> int:
    args = build_parser(default, prog).parse_args(argv)
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
