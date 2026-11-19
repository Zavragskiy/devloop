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
import difflib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from devloop import (
    __version__,
    baseline,
    config,
    findings,
    gitinfo,
    impact,
    mode,
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


def _argv_budget() -> int:
    """How much of the platform command line one invocation may occupy."""
    return max(1024, process.MAX_COMMAND_LINE - 1024)


def _batched(prefix: list[str], targets: list[str], budget: int | None = None) -> list[list[str]]:
    """``prefix`` and the targets split over as few invocations as the limit allows.

    A full-tree gate names every source and test file, and a few hundred paths
    already overflow the 32767-character Windows command line (``WinError 206``).
    Nothing about the file set changes: read in order, the batches carry exactly
    ``targets``, so the findings and the baseline keys are the same on every
    platform and for every tree size. Quoting is counted with the same routine
    ``subprocess`` uses, so the measurement is the one ``CreateProcess`` sees.
    """
    limit = _argv_budget() if budget is None else budget
    batches: list[list[str]] = []
    current = list(prefix)
    length = process.command_length(current)
    for target in targets:
        cost = process.command_length([target]) + 1
        if len(current) > len(prefix) and length + cost > limit:
            batches.append(current)
            current = list(prefix)
            length = process.command_length(current)
        current.append(target)
        length += cost
    batches.append(current)
    return batches


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


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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
        self.deferred: dict[str, int] = {}
        self.deferred_steps: list[str] = []
        self.unparsed: set[str] = set()

    @property
    def stopped(self) -> bool:
        """Only a blocking failure stops the run; a deferred step never does."""
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
        _unlink(path)
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

    def run_batched(
        self, label: str, commands: list[list[str]], *, defer_status: bool = False
    ) -> bool:
        """Run one step over several argument batches and merge what they reported.

        ``commands`` is what :func:`_batched` produced: the same tool called with
        a slice of the gated paths each time. One batch is exactly :meth:`run`.
        More than one accumulates the outputs in order and joins the per-batch
        finding files behind this step's single report path, so the concatenated
        ``ruff`` JSON still parses as one stream and a project command that
        appends to ``DEVLOOP_FINDINGS_FILE`` keeps every batch's entries. The
        caller sees one step: one header, one timing line, one failure.
        """
        if self.stopped or not commands:
            return True
        if len(commands) == 1:
            return self.run(label, commands[0], defer_status=defer_status)
        self.ran.add(label)
        print(f"[verify] {label} ...")
        started = time.monotonic()
        path = self.report_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        chunks: list[str] = []
        passed = True
        for number, command in enumerate(commands, 1):
            part = path.with_name(f"{path.stem}.{number}{path.suffix}")
            _unlink(part)
            parts.append(part)
            env = self._environment(label)
            env["DEVLOOP_FINDINGS_FILE"] = str(part)
            result = process.run(command, cwd=self.cfg.root, env=env)
            chunk = result.stdout + result.stderr
            chunks.append(chunk if not chunk or chunk.endswith("\n") else chunk + "\n")
            passed = passed and result.returncode == 0
        elapsed = time.monotonic() - started
        self.seconds += elapsed
        _unlink(path)
        with path.open("w", encoding="utf-8") as merged:
            for part in parts:
                if part.is_file():
                    merged.write(part.read_text(encoding="utf-8"))
                _unlink(part)
        output = "".join(chunks)
        self.outputs[label] = output
        if output and label not in self.quiet:
            print(output, end="" if output.endswith("\n") else "\n")
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

    def defer(self, label: str, count: int | None) -> None:
        """Clear a draft step's failure and count it as deferred.

        ``count`` is the number of deferred findings, or ``None`` for a step
        whose verdict is only its exit code (``policy``, the tests).
        """
        self.failures[:] = [failure for failure in self.failures if failure != label]
        if count is None:
            self.deferred_steps.append(label)
            word = "deferred"
        else:
            self.deferred[label] = count
            word = f"{count} deferred"
        pending = self.pending.pop(label, None)
        timing = f" ({pending[1]:.1f}s)" if pending is not None else ""
        print(f"[verify] {label}: {word}{timing}")

    def failure_output(self, label: str) -> str:
        """The captured output of a quiet step, for a failure that parsed nothing."""
        return self.outputs.get(label, "")


def _collected(steps: Steps, label: str) -> list[findings.Finding]:
    collect = getattr(steps, "collect", None)
    return collect(label) if callable(collect) else []


PARSED_STEPS = frozenset({"ruff", "format", "mypy"})
"""Steps whose findings devloop parses and reprints itself."""


@dataclass(frozen=True)
class Draft:
    """A draft run: what still blocks, and whether deferred detail is printed."""

    blocking: mode.Blocking
    show: bool = False


def _settle(
    steps: Steps,
    label: str,
    found: list[findings.Finding],
    entries: dict[str, set[str]],
    full: bool,
    *,
    detail: bool = False,
    draft: Draft | None = None,
) -> None:
    """Decide one step by what a baseline already accepts.

    In draft a new finding blocks only when ``draft_blocking`` (or a syntax
    error) says so; the rest is deferred. A step that failed without any
    finding falls through to the strict path and stays a failure.
    """
    if label not in steps.ran:
        return
    comparison = baseline.compare(entries, {label: found}, [label], full=full)
    fresh = list(comparison.new.get(label, ()))
    known_keys = entries.get(label, set())
    known = len({finding.key for finding in found if finding.key in known_keys})
    if draft is not None and fresh:
        blocking = [finding for finding in fresh if draft.blocking.blocks(label, finding)]
        deferred = [finding for finding in fresh if finding not in blocking]
        if deferred and draft.show:
            report.deferred_findings(label, deferred, detail=detail)
            if label not in PARSED_STEPS:
                _print_tail(steps.failure_output(label))
        if not blocking:
            steps.defer(label, len(deferred))
            return
        report.new_findings(label, blocking, known, detail=detail)
        if deferred:
            steps.deferred[label] = len(deferred)
            print(f"[verify] {label}: {len(deferred)} more deferred")
        steps.settle(label, ok=False)
        return
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
    draft: Draft | None = None,
) -> None:
    """Run the static gates; ``draft`` needs ``entries`` (``{}`` without a baseline)."""
    gated = _all_python(cfg) if full else files
    if gated:
        if entries is None:
            steps.run_batched("ruff", _batched(_python(cfg, "-m", "ruff", "check"), gated))
        else:
            steps.run_batched(
                "ruff",
                _batched(
                    _python(cfg, "-m", "ruff", "check", "--output-format", "json", "--exit-zero"),
                    gated,
                ),
                defer_status=True,
            )
            found = findings.parse_ruff(steps.outputs.get("ruff", ""), cfg.root)
            steps.unparsed.update(_unparsed(found))
            _settle(steps, "ruff", found, entries, full, detail=True, draft=draft)
        # A file that does not parse makes ``ruff format`` fail without a
        # ``Would reformat`` line, which reads as a broken tool. The syntax
        # error already blocks, so the formatter never sees that file.
        if not steps.stopped:
            formattable = [path for path in gated if path not in steps.unparsed]
            if formattable:
                steps.run_batched(
                    "format",
                    _batched(_python(cfg, "-m", "ruff", "format", "--check"), formattable),
                    defer_status=entries is not None,
                )
                if entries is not None:
                    found = findings.parse_ruff_format(steps.outputs.get("format", ""), cfg.root)
                    _settle(steps, "format", found, entries, full, draft=draft)
            _skip_unparsed(len(gated) - len(formattable))
    if cfg.quality is not None and gated:
        steps.run_batched(
            "quality",
            _batched(_python(cfg, str(cfg.quality)), gated),
            defer_status=entries is not None,
        )
        if entries is not None:
            _settle(steps, "quality", _collected(steps, "quality"), entries, full, draft=draft)
    if cfg.policy is not None:
        _policy_step(cfg, steps, draft)
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
                _settle(steps, "mypy", found, entries, full, detail=True, draft=draft)
    for spec in cfg.checks:
        _run_check(cfg, steps, spec, files, entries, full, draft=draft)


def _unparsed(found: list[findings.Finding]) -> set[str]:
    """Paths whose ruff finding is a syntax error, so later steps cannot parse them."""
    return {
        finding.key.rsplit(":", 1)[0]
        for finding in found
        if finding.key.rsplit(":", 1)[-1] in mode.SYNTAX_CODES
    }


def _skip_unparsed(count: int) -> None:
    if count:
        noun = "file" if count == 1 else "files"
        print(f"[verify] format: skipped {count} unparsed {noun}")


def _policy_step(cfg: Config, steps: Steps, draft: Draft | None) -> None:
    """The policy gate writes no keys, so in draft its exit code is deferred whole."""
    assert cfg.policy is not None
    command = _python(cfg, str(cfg.policy))
    if draft is None or "policy" in draft.blocking.steps:
        steps.run("policy", command)
        return
    if steps.run("policy", command, defer_status=True):
        steps.settle("policy", ok=True)
        return
    if draft.show:
        _print_tail(steps.failure_output("policy"))
    steps.defer("policy", None)


def _print_tail(output: str) -> None:
    for line in _tail(output).splitlines():
        print(f"  {line}")


def _run_check(
    cfg: Config,
    steps: Steps,
    spec: CheckSpec,
    files: list[str],
    entries: dict[str, set[str]] | None,
    full: bool,
    *,
    defer_status: bool = False,
    draft: Draft | None = None,
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
    command = cfg.resolve_command(spec.command)
    deferred = defer_status or entries is not None
    steps.run_batched(spec.name, _batched(command, targets), defer_status=deferred)
    if entries is not None:
        _settle(steps, spec.name, _collected(steps, spec.name), entries, full, draft=draft)


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
    cfg: Config,
    steps: Steps,
    changes: list[gitinfo.Change],
    args: argparse.Namespace,
    draft: Draft | None = None,
) -> None:
    if steps.unparsed and not args.no_tests:
        print("[verify] tests: not run, a file does not parse")
        return
    command = _test_command(cfg, changes, args)
    if command is None:
        print("[verify] tests: skipped")
        return
    if steps.stopped:
        print("[verify] tests: not run, a static step failed")
        print("[verify] --keep-going runs them anyway; --tests-only skips the static steps")
        return
    if draft is None or "tests" in draft.blocking.steps:
        if not steps.run("tests", command):
            report.test_failures(cfg)
        return
    if steps.run("tests", command, defer_status=True):
        steps.settle("tests", ok=True)
        return
    if draft.show:
        _print_tail(steps.failure_output("tests"))
        report.test_failures(cfg)
    steps.defer("tests", None)


def _explicit_selection(args: argparse.Namespace) -> bool:
    """True when the caller named the tests, so selection is not run."""
    return bool(args.files or args.only or args.last_failed)


def _checked_files(cfg: Config, paths: list[str]) -> None:
    """Refuse an explicit ``--files`` list that cannot name test files.

    The paths reach the runner verbatim, so a directory or a typo arrives there
    unchanged. A runner that skips what it does not recognise then warns on its
    own output and the run still reports ``tests: ok`` with nothing selected, so
    the selection is checked here: each path must exist and be a Python file.
    The check runs before the static steps, so the run fails fast instead of
    spending a minute on ruff and mypy before the tests select nothing.
    """
    problems = [text for raw in paths if (text := _file_problem(cfg, raw)) is not None]
    if problems:
        raise ValueError("--files must name existing test files:\n" + "\n".join(problems))


def _file_problem(cfg: Config, raw: str) -> str | None:
    """The reason ``raw`` cannot be a test file, or ``None`` when it can."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cfg.root / path
    if not path.exists():
        return f"  {raw}: no such file{_near_test(cfg, raw)}"
    if path.is_dir():
        return f"  {raw}: is a directory, not a test file{_inside(path, cfg)}"
    if path.suffix != ".py":
        return f"  {raw}: not a Python file"
    return None


def _inside(path: Path, cfg: Config) -> str:
    """A hint naming the test files a directory holds."""
    found = sorted(
        child
        for child in path.rglob("*.py")
        if child.name.startswith("test_") or child.name.endswith("_test.py")
    )
    if not found:
        return "; it holds no test file"
    shown = ", ".join(_relative(cfg, child) for child in found[:3])
    rest = "" if len(found) <= 3 else f" (and {len(found) - 3} more)"
    return f"; it holds test files: {shown}{rest}"


def _relative(cfg: Config, path: Path) -> str:
    """``path`` from the project root, even when it lies outside it."""
    try:
        return cfg.relative(path)
    except ValueError:
        return path.as_posix()


def _near_test(cfg: Config, raw: str) -> str:
    """``; did you mean …`` for a path that looks like a typo of a test file.

    A test file with the same name elsewhere is a likely intended target; after
    that, close names and then close paths are tried, both with a high cutoff so
    an unrelated test is never offered as the fix.
    """
    candidates = impact.all_tests(cfg)
    name = Path(raw).name
    same = [path for path in candidates if Path(path).name == name]
    if same:
        return f"; did you mean {same[0]}"
    by_name = {Path(path).name: path for path in candidates}
    close = difflib.get_close_matches(name, list(by_name), n=1, cutoff=0.8)
    if close:
        return f"; did you mean {by_name[close[0]]}"
    near = difflib.get_close_matches(raw.replace("\\", "/"), candidates, n=1, cutoff=0.85)
    return f"; did you mean {near[0]}" if near else ""


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


def _finish(steps: Steps, draft: bool = False) -> int:
    deferred = _deferred_summary(steps) if draft else ""
    if steps.failures:
        marker = ""
        if draft:
            marker = f" (draft; also deferred: {deferred})" if deferred else " (draft)"
        print(f"[verify] FAILED{marker} in {steps.seconds:.1f}s")
        print(f"[verify] failed: {', '.join(steps.failures)}")
        return 1
    if not draft:
        print(f"[verify] ok in {steps.seconds:.1f}s")
    elif deferred:
        print(
            f"[verify] ok (draft; deferred: {deferred} - they block at commit, nothing to do now)"
            f" in {steps.seconds:.1f}s"
        )
    else:
        print(f"[verify] ok (draft; nothing deferred) in {steps.seconds:.1f}s")
    return 0


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _deferred_summary(steps: Steps) -> str:
    """What a draft run deferred, findings and whole steps never mixed; ``""`` for none."""
    counted = {label: count for label, count in steps.deferred.items() if count}
    parts: list[str] = []
    if counted:
        detail = ", ".join(f"{label} {count}" for label, count in counted.items())
        parts.append(f"{_plural(sum(counted.values()), 'finding')} ({detail})")
    if steps.deferred_steps:
        names = ", ".join(steps.deferred_steps)
        parts.append(f"{_plural(len(steps.deferred_steps), 'step')} ({names})")
    return ", ".join(parts)


def _cache_key(cfg: Config, args: argparse.Namespace) -> str | None:
    """The tree key when this run may be replayed from the cache, else ``None``."""
    cacheable = (
        args.full
        and not args.base
        and not args.no_cache
        and not args.no_tests
        and not args.tests_only
    )
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
    if args.tests_only and args.no_tests:
        raise ValueError("--tests-only and --no-tests contradict each other")
    current = mode.resolve(cfg, args.mode, full=args.full)
    if args.files and not args.no_tests:
        _checked_files(cfg, args.files)
    changes = gitinfo.collect(cfg.root, args.base)
    files = _changed_python(cfg, changes)
    markers = [name for name, on in (("full", args.full), ("tests only", args.tests_only)) if on]
    scope = f" ({', '.join(markers)})" if markers else ""
    print(f"[verify] {_changed_count(changes)} changed, {len(files)} Python file(s){scope}")
    if not current.quiet_default:
        print(f"[verify] {current.header}")

    key = _cache_key(cfg, args)
    if key is not None and verdict.recent(cfg, key):
        print("[verify] this tree already passed the full gate; nothing to re-check")
        return 0
    entries = _baseline_entries(cfg)
    draft: Draft | None = None
    if current.draft:
        draft = Draft(mode.Blocking.parse(cfg.draft_blocking), args.show_deferred)
        entries = {} if entries is None else entries
    # Draft keeps going past a blocking finding: the deferred total stays complete
    # and the tests still report, so "tests: not run" never appears in draft.
    keep_going = args.keep_going or draft is not None
    steps = Steps(cfg, keep_going, quiet=_quiet(cfg, entries, draft))
    if not args.tests_only:
        _static_steps(
            cfg,
            steps,
            files,
            args.full,
            entries=entries,
            changed_only=args.changed_only or cfg.mypy_changed_only,
            draft=draft,
        )
    _test_step(cfg, steps, changes, args, draft)
    code = _finish(steps, draft=draft is not None)
    if code == 0 and key is not None:
        verdict.remember(cfg, key)
    return code


def _quiet(cfg: Config, entries: dict[str, set[str]] | None, draft: Draft | None) -> frozenset[str]:
    """Steps whose own output is captured instead of forwarded.

    With parsed findings devloop reprints what matters; in draft a step that
    does not block is silenced entirely, so its output cannot read as a failure.
    ``--show-deferred`` reprints a deferred step's output, never a passing one's.
    """
    quiet = set(PARSED_STEPS) if entries is not None else set()
    if draft is not None:
        candidates = {"quality", "policy", "tests", *(spec.name for spec in cfg.checks)}
        quiet |= candidates - draft.blocking.steps
    return frozenset(quiet)


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
        steps.run_batched(
            "ruff",
            _batched(
                _python(cfg, "-m", "ruff", "check", "--output-format", "json", "--exit-zero"),
                targets,
            ),
        )
        try:
            observed["ruff"] = findings.parse_ruff(steps.outputs.get("ruff", ""), cfg.root)
        except ValueError as error:
            print(f"devloop: {error}", file=sys.stderr)
            return None
        _collect_verdict(steps, "ruff", observed["ruff"], broken)
        steps.run_batched(
            "format",
            _batched(_python(cfg, "-m", "ruff", "format", "--check"), targets),
            defer_status=True,
        )
        observed["format"] = findings.parse_ruff_format(steps.outputs.get("format", ""), cfg.root)
        _collect_verdict(steps, "format", observed["format"], broken)
        command = _mypy_command(cfg, targets, changed_only=False, baseline_mode=True)
        if command is not None:
            steps.run("mypy", command, defer_status=True)
            observed["mypy"] = findings.parse_mypy(steps.outputs.get("mypy", ""), cfg.root)
            _collect_verdict(steps, "mypy", observed["mypy"], broken)
    if cfg.quality is not None and targets:
        steps.run_batched(
            "quality", _batched(_python(cfg, str(cfg.quality)), targets), defer_status=True
        )
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
    current = mode.resolve(cfg, args.mode)
    if current.draft and not args.check:
        raise ValueError(
            f"baseline is refused: mode draft via {_mode_origin(cfg, current)}; draft findings "
            "are not accepted debt (devloop mode strict, or --mode strict for this run)"
        )
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


def _mode_origin(cfg: Config, current: mode.Mode) -> str:
    if current.source == "file":
        return _relative(cfg, mode.mode_file(cfg))
    if current.source == "default":
        return "[tool.devloop.modes] default"
    return current.source


def run_mode(args: argparse.Namespace) -> int:
    """Pin this checkout to a mode (or drop the pin) and print how it resolves."""
    cfg = config.load()
    if args.value is not None:
        mode.write_file(cfg, None if args.value == "default" else args.value)
    current = mode.resolve(cfg, args.mode)
    print(f"[verify] {current.header}")
    for source, value in mode.chain(cfg, args.mode):
        label = _relative(cfg, mode.mode_file(cfg)) if source == "file" else source
        print(f"  {label}: {value or '-'}")
    print(f"  CI: {os.environ.get('CI') or '-'}")
    print(f"  draft_blocking: {', '.join(cfg.draft_blocking) or '-'}")
    print(f"  devloop: {__version__}")
    _check_ruff_prefixes(cfg)
    return 0


def _check_ruff_prefixes(cfg: Config) -> None:
    """Warn about a ``draft_blocking`` ruff prefix that matches no installed rule."""
    blocking = mode.Blocking.parse(cfg.draft_blocking)
    if not blocking.ruff_codes:
        return
    command = _python(cfg, "-m", "ruff", "rule", "--all", "--output-format", "json")
    result = process.run(command, cwd=cfg.root, env={**os.environ, **cfg.environment})
    try:
        rules = json.loads(result.stdout)
        codes = [str(rule["code"]) for rule in rules]
    except (ValueError, TypeError, KeyError):
        print("[verify] draft_blocking: could not list the ruff rules to check the prefixes")
        return
    for prefix in mode.unmatched_prefixes(blocking, codes):
        print(
            f"[verify] draft_blocking: ruff:{prefix} matches no ruff rule, so it blocks "
            "nothing (ruff rule --all lists the codes)"
        )


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

The static steps stop the run when one of them fails, and the tests are then
not run either; --keep-going reports every failing step instead and still runs
them. A file that does not parse is the exception once ruff findings are parsed
(a baseline, or draft): its formatting is skipped and the tests are not run,
because the suite cannot import it. --tests-only skips the static steps
altogether and runs only the selected tests. --tests-only is the red/green loop
on a file that is not formatted yet; --keep-going --full is the commit gate
that keeps going.

A baseline file (devloop-baseline.json unless configured otherwise) freezes the
findings a tree already has: check fails only on new ones, and --full names the
entries that no longer match. devloop baseline rewrites it from the whole tree;
devloop baseline --check fails when a new finding or a stale entry appears, so
CI can keep the accepted debt from rotting.

Draft mode is for prototyping: devloop mode draft pins it for this checkout,
--mode draft sets it for one run. A plain check runs every step except what
cannot parse (draft implies --keep-going) and fails only on a file that does
not parse, a tool that breaks without a finding and what [tool.devloop.modes]
draft_blocking lists (undefined names and the affected tests by default);
everything else is counted on the last line as deferred and blocks at --full,
which is always strict. --show-deferred prints the deferred detail; --explain
stays the test selection. devloop mode default drops the pin.
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
        choices=("check", "baseline", "impact", "changed", "select-tests", "mode"),
        help="check runs the pipeline (default); baseline freezes today's findings; "
        "impact prints the selected test files; changed lists the git changes the "
        "selection sees; select-tests prints the test files the built-in selector picks; "
        "mode prints or pins the working mode",
    )
    parser.add_argument(
        "value",
        nargs="?",
        choices=("draft", "strict", "default"),
        help="mode command: pin this checkout to draft or strict; default removes the pin",
    )
    parser.add_argument(
        "--mode",
        choices=config.MODES,
        default=None,
        help="the mode of this run, over DEVLOOP_MODE, the pinned mode and the configured "
        "default; --full is always strict",
    )
    parser.add_argument(
        "--show-deferred",
        action="store_true",
        help="draft mode: print the deferred findings and the output of deferred steps",
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
        help="run only these test files (implies an explicit selection); each path must be "
        "an existing test file, checked before the run",
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
        "--keep-going",
        action="store_true",
        help="do not stop at the first failure: report every failing step and still run "
        "the tests when a static step failed",
    )
    parser.add_argument(
        "--tests-only",
        action="store_true",
        help="skip every static step (ruff, format, quality, policy, mypy) and run only "
        "the selected tests",
    )
    parser.add_argument("--explain", action="store_true", help="print the selected test files")
    return parser


def main(argv: list[str] | None = None, default: str = "check", prog: str = "devloop") -> int:
    parser = build_parser(default, prog)
    args = parser.parse_args(argv)
    if args.value is not None and args.command != "mode":
        parser.error(f"{args.value!r} is only a value of the mode command")
    try:
        if args.command == "mode":
            return run_mode(args)
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
