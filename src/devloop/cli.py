"""The ``check``, ``baseline``, ``impact``, ``changed`` and ``select-tests`` commands.

``check`` is the pipeline: the static gates, then the tests affected by the
change. ``--full`` checks every tracked file and runs the whole suite; a plain
run is the edit loop. ``baseline`` freezes the findings a tree already has so
that ``check`` fails only on new ones. ``impact`` prints the selected files,
``changed`` lists the git entries the selection sees, and ``select-tests`` is the
built-in selector command (one test path per line) that ``check`` runs by
default.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from devloop import (
    baseline,
    config,
    findings,
    gitinfo,
    impact,
    process,
    report,
    selector,
    verdict,
)
from devloop.config import CheckSpec, Config


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
    return any(_is_under(relative, root) for root in cfg.source_roots)


def _is_under(relative: str, root: str) -> bool:
    root = root.rstrip("/")
    return relative == root or relative.startswith(root + "/")


def _slug(label: str) -> str:
    """A file-system-safe form of a step name."""
    clean = "".join(char if char.isalnum() or char in "-_" else "-" for char in label)
    return clean.strip("-") or "step"


def _tail(output: str, limit: int = 20) -> str:
    lines = output.rstrip().splitlines()
    return "\n".join(lines[-limit:])


class Steps:
    """Run verification steps in order, timing them and stopping on failure."""

    def __init__(self, cfg: Config, keep_going: bool, quiet: frozenset[str] = frozenset()):
        self.cfg = cfg
        self.keep_going = keep_going
        self.quiet = quiet
        self.failures: list[str] = []
        self.seconds: float = 0.0
        self.outputs: dict[str, str] = {}
        self.ran: set[str] = set()
        self.pending: dict[str, tuple[bool, float]] = {}

    @property
    def stopped(self) -> bool:
        return bool(self.failures) and not self.keep_going

    def report_path(self, label: str) -> Path:
        """Where a step writes the findings it wants the baseline to see."""
        return self.cfg.cache_dir / "findings" / f"{_slug(label)}.jsonl"

    def _environment(self, label: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.cfg.environment)
        env["DEVLOOP_FINDINGS_FILE"] = str(self.report_path(label))
        return env

    def run(self, label: str, command: list[str] | None, *, defer_status: bool = False) -> bool:
        """Run one step; True when it passed or was skipped.

        ``defer_status`` keeps the timing line back until :meth:`settle` knows
        whether the baseline accepts the findings; printing ``FAILED`` first and
        clearing it afterwards would leave a false failure in the log.
        """
        if command is None or self.stopped:
            return True
        self.ran.add(label)
        print(f"[verify] {label} ...")
        started = time.monotonic()
        path = self.report_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        result = process.run(command, cwd=self.cfg.root, env=self._environment(label))
        elapsed = time.monotonic() - started
        self.seconds += elapsed
        output = result.stdout + result.stderr
        self.outputs[label] = output
        if output and label not in self.quiet:
            print(output, end="" if output.endswith("\n") else "\n")
        passed = result.returncode == 0
        if defer_status:
            self.pending[label] = (passed, elapsed)
        else:
            print(f"[verify] {label}: {'ok' if passed else 'FAILED'} ({elapsed:.1f}s)")
        if not passed:
            self.failures.append(label)
        return passed

    def collect(self, label: str) -> list[findings.Finding]:
        """The findings a step wrote for this run, never a previous run's file."""
        if label not in self.ran:
            return []
        return findings.read_report(self.report_path(label), label)

    def settle(self, label: str, ok: bool) -> None:
        """Record or clear the failure of a step whose verdict is decided later."""
        if ok:
            self.failures[:] = [failure for failure in self.failures if failure != label]
        elif label not in self.failures:
            self.failures.append(label)
        pending = self.pending.pop(label, None)
        if pending is not None:
            print(f"[verify] {label}: {'ok' if ok else 'FAILED'} ({pending[1]:.1f}s)")

    def failure_output(self, label: str) -> str:
        """The captured output of a quiet step, for a failure that parsed nothing."""
        return self.outputs.get(label, "")


def _collected(steps: Steps, label: str) -> list[findings.Finding]:
    collect = getattr(steps, "collect", None)
    return collect(label) if callable(collect) else []


def _settle(
    steps: Steps,
    label: str,
    found: list[findings.Finding],
    entries: dict[str, set[str]],
    full: bool,
    *,
    detail: bool = False,
) -> None:
    """Decide one step by what a baseline already accepts."""
    if label not in steps.ran:
        return
    comparison = baseline.compare(entries, {label: found}, [label], full=full)
    fresh = list(comparison.new.get(label, ()))
    known_keys = entries.get(label, set())
    known = len({finding.key for finding in found if finding.key in known_keys})
    if fresh:
        report.new_findings(label, fresh, known, detail=detail)
        steps.settle(label, ok=False)
    elif found:
        report.new_findings(label, [], known, detail=detail)
        steps.settle(label, ok=True)
    elif label in steps.failures:
        output = steps.failure_output(label)
        if output.strip():
            print(f"[verify] {label}: the tool failed without a finding:")
            for line in _tail(output).splitlines():
                print(f"  {line}")
    stale = list(comparison.stale.get(label, ()))
    if stale:
        report.stale_entries(label, stale)
    # The timing line was held back by ``defer_status``; print it now that the
    # baseline has decided. This is also the only settle of a step that reported
    # no finding at all (ruff ``--exit-zero``, a clean formatter).
    steps.settle(label, ok=label not in steps.failures)


def _static_steps(
    cfg: Config,
    steps: Steps,
    files: list[str],
    full: bool,
    *,
    entries: dict[str, set[str]] | None = None,
    changed_only: bool = False,
) -> None:
    gated = _all_python(cfg) if full else files
    if gated:
        if entries is None:
            steps.run("ruff", _python(cfg, "-m", "ruff", "check", *gated))
        else:
            steps.run(
                "ruff",
                _python(
                    cfg, "-m", "ruff", "check", "--output-format", "json", "--exit-zero", *gated
                ),
                defer_status=True,
            )
            found = findings.parse_ruff(steps.outputs.get("ruff", ""), cfg.root)
            _settle(steps, "ruff", found, entries, full, detail=True)
        steps.run(
            "format",
            _python(cfg, "-m", "ruff", "format", "--check", *gated),
            defer_status=entries is not None,
        )
        if entries is not None:
            found = findings.parse_ruff_format(steps.outputs.get("format", ""), cfg.root)
            _settle(steps, "format", found, entries, full)
    if cfg.quality is not None and gated:
        steps.run(
            "quality",
            _python(cfg, str(cfg.quality), *gated),
            defer_status=entries is not None,
        )
        if entries is not None:
            _settle(steps, "quality", _collected(steps, "quality"), entries, full)
    if cfg.policy is not None:
        steps.run("policy", _python(cfg, str(cfg.policy)))
    if full or any(_is_source(cfg, path) for path in files):
        if changed_only and full:
            print("[verify] mypy: changed-only is ignored on --full, the whole scope is checked")
            changed_only = False
        frozen = entries is not None
        command = _mypy_command(cfg, files, changed_only=changed_only, baseline_mode=frozen)
        if command is None:
            print("[verify] mypy: skipped, no changed file inside the mypy scope")
        else:
            steps.run("mypy", command, defer_status=entries is not None)
            if entries is not None:
                found = findings.parse_mypy(steps.outputs.get("mypy", ""), cfg.root)
                _settle(steps, "mypy", found, entries, full, detail=True)
    for spec in cfg.checks:
        _run_check(cfg, steps, spec, files, entries, full)


def _run_check(
    cfg: Config,
    steps: Steps,
    spec: CheckSpec,
    files: list[str],
    entries: dict[str, set[str]] | None,
    full: bool,
    *,
    defer_status: bool = False,
) -> None:
    """Run one ``[[tool.devloop.check]]`` command with the paths it asked for."""
    if spec.paths == "changed":
        if not files:
            print(
                f"[verify] {spec.name}: skipped (no changed files; "
                'use paths = "all" for the whole tree)'
            )
            return
        targets = list(files)
    else:
        targets = _all_python(cfg) if spec.paths == "all" else []
    command = [*cfg.resolve_command(spec.command), *targets]
    steps.run(spec.name, command, defer_status=defer_status or entries is not None)
    if entries is not None:
        _settle(steps, spec.name, _collected(steps, spec.name), entries, full)


def _scope_targets(cfg: Config, files: list[str]) -> list[str]:
    if not cfg.mypy_scope:
        return list(files)
    return [path for path in files if any(_is_under(path, scope) for scope in cfg.mypy_scope)]


def _mypy_command(
    cfg: Config, files: list[str], *, changed_only: bool, baseline_mode: bool
) -> list[str] | None:
    """``mypy`` over the configured scope, or over the changed files only."""
    options = ["-m", "mypy"]
    if baseline_mode:
        options += ["--show-error-codes", "--no-error-summary"]
    if changed_only:
        targets = _scope_targets(cfg, files)
        return _python(cfg, *options, *targets) if targets else None
    return _python(cfg, *options, *cfg.mypy_scope)


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


def _cache_key(cfg: Config, args: argparse.Namespace) -> str | None:
    """The tree key when this run may be replayed from the cache, else ``None``."""
    cacheable = args.full and not args.base and not args.no_cache and not args.no_tests
    if not cacheable or _explicit_selection(args):
        return None
    return verdict.tree_key(cfg)


def _baseline_entries(cfg: Config) -> dict[str, set[str]] | None:
    """The committed baseline, or ``None`` when the project does not use one."""
    if cfg.baseline is None or not cfg.baseline.is_file():
        return None
    return baseline.load(cfg.baseline)


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
    entries = _baseline_entries(cfg)
    quiet = frozenset({"ruff", "format", "mypy"}) if entries is not None else frozenset()
    steps = Steps(cfg, args.keep_going, quiet=quiet)
    _static_steps(
        cfg,
        steps,
        files,
        args.full,
        entries=entries,
        changed_only=args.changed_only or cfg.mypy_changed_only,
    )
    _test_step(cfg, steps, changes, args)
    code = _finish(steps)
    if code == 0 and key is not None:
        verdict.remember(cfg, key)
    return code


def _changed_count(changes: list[gitinfo.Change]) -> int:
    return sum(1 for change in changes if not change.deleted)


def _changed_python(cfg: Config, changes: list[gitinfo.Change]) -> list[str]:
    return [
        change.path
        for change in changes
        if not change.deleted and change.path.endswith(".py") and (cfg.root / change.path).is_file()
    ]


def _collect_findings(cfg: Config, steps: Steps) -> dict[str, list[findings.Finding]] | None:
    """Run every finding step over the whole tree, or ``None`` when one broke."""
    targets = _all_python(cfg)
    observed: dict[str, list[findings.Finding]] = {}
    broken: list[str] = []
    if targets:
        steps.run(
            "ruff",
            _python(cfg, "-m", "ruff", "check", "--output-format", "json", "--exit-zero", *targets),
        )
        try:
            observed["ruff"] = findings.parse_ruff(steps.outputs.get("ruff", ""), cfg.root)
        except ValueError as error:
            print(f"devloop: {error}", file=sys.stderr)
            return None
        _collect_verdict(steps, "ruff", observed["ruff"], broken)
        steps.run(
            "format", _python(cfg, "-m", "ruff", "format", "--check", *targets), defer_status=True
        )
        observed["format"] = findings.parse_ruff_format(steps.outputs.get("format", ""), cfg.root)
        _collect_verdict(steps, "format", observed["format"], broken)
        command = _mypy_command(cfg, targets, changed_only=False, baseline_mode=True)
        if command is not None:
            steps.run("mypy", command, defer_status=True)
            observed["mypy"] = findings.parse_mypy(steps.outputs.get("mypy", ""), cfg.root)
            _collect_verdict(steps, "mypy", observed["mypy"], broken)
    if cfg.quality is not None and targets:
        steps.run("quality", _python(cfg, str(cfg.quality), *targets), defer_status=True)
        observed["quality"] = _collected(steps, "quality")
        _collect_verdict(steps, "quality", observed["quality"], broken)
    for spec in cfg.checks:
        _run_check(cfg, steps, spec, targets, None, True, defer_status=True)
        observed[spec.name] = _collected(steps, spec.name)
        _collect_verdict(steps, spec.name, observed[spec.name], broken)
    if broken:
        print(f"devloop: {', '.join(broken)} failed without a finding", file=sys.stderr)
        return None
    return observed


def _collect_verdict(
    steps: Steps, label: str, found: list[findings.Finding], broken: list[str]
) -> None:
    """Settle a collection step: findings are the point, a silent crash is not.

    ``Steps.run`` held its status line back, so this prints it: ``ok`` when the
    tool reported the findings it was asked for (its exit code is not a verdict
    while collecting), ``FAILED`` when it broke without parsing to anything.
    """
    crashed = label in steps.failures and not found
    if crashed:
        broken.append(label)
    steps.settle(label, ok=not crashed)


def _check_baseline(cfg: Config, observed: dict[str, list[findings.Finding]]) -> int:
    """Compare the tree with the baseline without writing; 1 when it drifted."""
    assert cfg.baseline is not None
    if not cfg.baseline.is_file():
        raise ValueError(f"no baseline at {cfg.baseline}; run devloop baseline first")
    entries = baseline.load(cfg.baseline)
    comparison = baseline.compare(entries, observed, [*observed, *entries], full=True)
    dirty = False
    for step in sorted({*observed, *entries}):
        found = observed.get(step, [])
        known_keys = entries.get(step, set())
        known = len({finding.key for finding in found if finding.key in known_keys})
        fresh = list(comparison.new.get(step, ()))
        stale = list(comparison.stale.get(step, ()))
        if fresh:
            report.new_findings(step, fresh, known, detail=step in {"ruff", "format", "mypy"})
            dirty = True
        if stale:
            report.stale_entries(step, stale)
            dirty = True
    if dirty:
        return 1
    total = sum(len(keys) for keys in entries.values())
    print(f"[verify] baseline is current ({total} {'entry' if total == 1 else 'entries'})")
    return 0


def run_baseline(args: argparse.Namespace) -> int:
    """Freeze the findings the whole tree has today into the baseline file."""
    cfg = config.load()
    if cfg.baseline is None:
        raise ValueError('the baseline is disabled by [tool.devloop] baseline = ""')
    quiet = frozenset({"ruff", "format", "mypy"})
    # Collecting is not gating: a step that reports findings is expected to exit
    # nonzero, and stopping there would drop every later step from the file.
    steps = Steps(cfg, keep_going=True, quiet=quiet)
    observed = _collect_findings(cfg, steps)
    if observed is None:
        return 2
    if getattr(args, "check", False):
        return _check_baseline(cfg, observed)
    baseline.save(
        cfg.baseline, {step: [found.key for found in items] for step, items in observed.items()}
    )
    for step in sorted(observed):
        print(f"[verify] {step}: {len({finding.key for finding in observed[step]})} finding(s)")
    print(f"[verify] baseline written to {cfg.baseline}")
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

A baseline file (devloop-baseline.json unless configured otherwise) freezes the
findings a tree already has: check fails only on new ones, and --full names the
entries that no longer match. devloop baseline rewrites it from the whole tree;
devloop baseline --check fails when a new finding or a stale entry appears, so
CI can keep the accepted debt from rotting.
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
        choices=("check", "baseline", "impact", "changed", "select-tests"),
        help="check runs the pipeline (default); baseline freezes today's findings; "
        "impact prints the selected test files; changed lists the git changes the "
        "selection sees; select-tests prints the test files the built-in selector picks",
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
        "--changed-only",
        action="store_true",
        help="mypy checks the changed files inside the configured scope (changed mode; "
        "ignored on --full)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="baseline command: compare the tree with the baseline and fail on a new or "
        "stale entry, without writing",
    )
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
        if args.command == "baseline":
            return run_baseline(args)
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
