# Design

`devloop` is a thin pipeline over four project-supplied commands. This document
records why it looks like that, so the next reader does not re-litigate it.

## Static selection, not coverage

The selector builds an import graph from the AST of the source roots and the
tests root, then walks reverse edges from the changed modules. There is no
coverage database, no `pytest-cov` in the loop and nothing to keep fresh, so a
fresh checkout selects correctly. The cost is coarse selection: a package
`__init__` change selects its whole package, and a module change selects every
test that imports it directly or transitively. On a repository of ~100 modules
and ~1000 tests the warm graph build costs about 0.6 s and the runner starts
immediately.

The graph is cached by content hash in `<cache_dir>/import-graph.json`. A cache
miss or a corrupt cache only costs a rebuild; it never changes a result.

## The selector is a command, not a library import

`impact.select` runs `[tool.devloop].selector` as a subprocess and reads one test
path per stdout line. That keeps the wrapper independent of the engine: the
built-in `devloop select-tests` is only the default, and `impacted-tests`,
`pytest-selective` or a project script can replace it. It also means the selector
can fail — and a failing selector must never make the run *weaker*.

## Fail open, never fail closed

Every uncertain case runs the full suite instead of a guess:

* the selector exits non-zero, prints nothing, or prints a path that is not an
  existing test file;
* a `conftest.py` changed, a configured `shared` input changed, a non-Python
  asset under a source root changed, or a source or test file was deleted;
* a Python file could not be parsed (`unparsed` in the graph);
* the selection reaches `max_fraction` of the suite.

Selection is allowed to *add* tests only. `always` lists the guard tests that
scan the tree and must run for any change. The full-suite run before a commit is
the safety net for dynamic imports that static analysis cannot see.

## Cache by content hash, verify by tree state

Two caches exist, both pure optimisations:

| Cache | Key | Purpose |
| --- | --- | --- |
| `<cache_dir>/import-graph.json` | content hash of every source and test file | skip re-parsing |
| `<cache_dir>/verify-full.json` | commit + staged/unstaged diff + untracked file contents | replay a green `--full` |

A stale cache entry can only cost time. The tree key deliberately includes
untracked file contents, so editing a new file invalidates the verdict, while
ignored paths (`tmp/`, `.devloop/`) do not.

## A baseline is a ratchet, not a switch

Turning a gate off for a legacy tree and turning it on once the debt is paid is a
trade nobody makes: the debt never gets paid. A baseline flips the default
instead. The findings the tree has today are frozen in one committed file, and
the gate is red only on what a change adds, which is the same shape as an inline
`# noqa` but reviewable in one place.

Three decisions follow from that:

* keys are `<path>:<code>`, not `<code>`. A code-wide baseline would accept the
  next file that repeats the mistake; a file-wide one is the unit a reviewer can
  reason about.
* keys are matched as a set, not counted. A second occurrence of an accepted
  finding in the same file is not the ratchet's job; the count would need a
  re-baseline after every unrelated edit and would make the file noisy.
* stale entries are computed only on `--full`. A changed-file run sees a subset
  of the tree; treating the rest as stale is the bug that makes a baseline
  unusable from a pre-commit hook.

The baseline never weakens the safety net: a missing file means the mechanism is
off, and an unreadable one is an error, because silently reading it as empty
would turn the ratchet back into an off switch. A tool that fails and prints
nothing parseable fails the step — an unparsed failure is not a baselined one.

## Adapters are commands, not plugins

A plugin API would put the wrapper and the project on the same object graph and
tie their versions together. A command does not: `quality`, `policy` and `runner`
are paths invoked with the project's own interpreter, so a project keeps its
existing gate scripts and the tool stays dependency-free. The one contract the
runner may satisfy beyond an exit code is the optional summary JSON that
`devloop.report` reads for the failure output.

The baseline adds a second optional contract, and it is a file rather than an
import: a command that wants its findings frozen reads `DEVLOOP_FINDINGS_FILE`
from the environment and appends one JSON object per finding. Commands that
ignore it keep their exit-code semantics, so no existing adapter changes.

## Why not an off-the-shelf selector?

Evaluated in November 2026 on a PyQt6/VTK repository (~100 source modules, ~1000
tests):

| Tool | Result |
| --- | --- |
| `pytest-impacted` 0.37 | The design matches, and the Rust extra parses in ~2 s, but the wheel ships a top-level `tests` package in site-packages; it shadows the repository's `tests` namespace and breaks `import tests.robotics`. Rejected until packaging is fixed. |
| `pytest-selective` 1.5 | Installs cleanly, but `pytest --selective --collect-only` costs ~7 s (it imports the app to collect), and the pre-collection CLI needs a JSON config. Too slow for the inner loop. |
| `pytest-testmon` / `pytest-tia` | Coverage-based: require a full recorded run, a fresh database, and they interact badly with per-shard subprocesses. Better suited to CI than to a fresh checkout. |

The built-in selector has no dependencies and no import side effects. If one of
the tools above fixes its packaging or speed, only the `selector` line in
`[tool.devloop]` changes.
