# devloop

[![ci](https://github.com/Zavragskiy/devloop/actions/workflows/ci.yml/badge.svg)](https://github.com/Zavragskiy/devloop/actions/workflows/ci.yml)

A fast verification loop for Python projects. `devloop check` finds the changed
files, runs Ruff, an optional project quality gate, an optional whole-tree policy
gate, mypy when source changed, and the tests affected by the change.
`devloop check --full` covers every source and test file and runs the complete
test suite.

```
changed files ─┐
               ├─ ruff · format · quality · policy · mypy · affected tests
[tool.devloop] ┘
                 check         → the edit loop, seconds
                 check --full  → the commit / stage gate
                 baseline      → freeze today's findings, fail only on new ones
                 impact        → which tests a change would run
```

The installed package uses only the Python standard library (Python 3.11+).
Ruff, mypy, pytest and the project's own gates are supplied by the **project
being checked**. The default test selector analyzes Python imports statically:
it needs no coverage database, no recorded run and no warm cache, so a fresh
checkout works.

## Install

Development, from a local checkout:

```powershell
poetry add --group dev --editable D:/Projects/devloop
poetry add --group dev ruff mypy pytest
poetry run devloop --help
```

Pinned to a commit for a team or CI:

```toml
[tool.poetry.group.dev.dependencies]
devloop = { git = "https://github.com/Zavragskiy/devloop.git", rev = "<commit-sha>" }
```

Keep `devloop` in the development dependency group: developers and CI get it,
the application runtime does not. No code is copied into the consuming project.
See [Integration](docs/integration.md) for pip, custom adapters and CI notes.

## Configure

Add `[tool.devloop]` to the consuming project's `pyproject.toml`. `source_roots`
is required and lists the source paths relative to the project root:

```toml
[tool.devloop]
source_roots = ["src/my_package"]
tests_root = "tests"
cache_dir = ".devloop"
runner = "tools/run_tests.py"
quality = "tools/check_quality.py"
policy = "tools/check_package_policy.py"
shared = ["pyproject.toml", "poetry.lock"]
always = ["tests/test_contract.py"]
max_fraction = 0.6
baseline = "devloop-baseline.json"
```

Ignore the cache directory in the consuming project's `.gitignore`. Every key
except `source_roots` has a working default; [Integration](docs/integration.md)
documents all of them and the four adapter contracts (selector, quality, policy,
runner).

## Run

From the consuming project's root, in its own environment:

| Command | Purpose |
| --- | --- |
| `devloop check` | changed files: ruff, format, quality, policy, mypy, affected tests |
| `devloop check --explain` | the same, printing the selected test files |
| `devloop check --full` | every tracked file and the whole suite (commit / stage gate) |
| `devloop check --files <paths>` | gate the change, run only these test files |
| `devloop check --only <token>` | gate the change, run only matching test files |
| `devloop check --last-failed` | gate the change, replay the last run's failures |
| `devloop check --base origin/main` | a branch diff instead of the working tree |
| `devloop check --changed-only` | mypy over the changed files inside its scope |
| `devloop baseline` | freeze the findings the tree has today |
| `devloop impact` | print the affected test files, run nothing |
| `devloop changed` | list the git entries the selection sees |
| `devloop select-tests` | the built-in selector: one affected test path per line |

`check` is the default command, so `devloop` alone works. `--jobs N` forwards
shard parallelism, `--no-tests` skips the test step, `--keep-going` reports every
failing step instead of stopping at the first, `--no-cache` ignores a recorded
green verdict.

### Two loops, one command

The default changed mode is the edit loop: it gates the files you touched and
the tests they affect, and returns in seconds. `--full` is the commit and stage
gate: it gates every tracked file and runs the whole suite. Use `--full` before a
commit or at the end of a stage, not after every edit.

### Selection safety

The selector follows imports and literal `patch(...)` and `import_module(...)`
targets. It includes tests that depend on changed modules, directly changed test
files, and any configured `always` tests. When it cannot make a reliable narrow
choice, it selects the full suite: after a selector failure, an invalid or empty
selection, a changed `conftest.py`, a shared input or source asset change, a
deleted source or test file, an unparseable source file, or a selection at or
above `max_fraction`. Selection only ever *adds* tests; it never narrows a
full-suite decision.

### What a failure tells you

A custom runner may record its shards in `<cache_dir>/run-tests.json` and leave
the captured output of a failing shard in `<cache_dir>/shard-<name>.log`. When
the test step fails, `check` prints the failing test ids and the tail of each
such log, so the traceback is on screen without a second command. The same two
artefacts keep `--last-failed` working across runs.

### A green tree is remembered

`--full` is the expensive step, and running it twice on an unchanged tree cannot
find anything new. A green full run records a key over the commit, the staged and
unstaged diff, and every untracked file's content; the next `--full` on the same
key answers `[verify] this tree already passed the full gate` in about a second.
Any edit, stage or revert changes the key, ignored paths do not, and
`--no-cache` ignores the record entirely.

### The debt you already have is not a blocker

`devloop baseline` runs ruff, mypy and the configured quality gate over the whole
tree and writes `devloop-baseline.json`. After that `check` fails only on a
finding the file does not list, and `--full` prints the entries that no longer
match anything — a warning, never a failure. Ruff and mypy keys are
`<path>:<code>`, so the ratchet is per file and per code; a quality script opts
in by appending its own keys to `$DEVLOOP_FINDINGS_FILE`.

### The steps get what the project needs

`[tool.devloop.env]` passes variables such as `QT_QPA_PLATFORM=offscreen` to every
step subprocess, and `[[tool.devloop.check]]` runs an extra project command — an
acceptance shard, a plan-run step — inside the same pipeline, with
`paths = "none"`, `"changed"` or `"all"`. The mypy scope that grows package by
package lives in `[tool.devloop.mypy]`. See
[Integration](docs/integration.md).

## Hosting

GitHub is the primary home: <https://github.com/Zavragskiy/devloop>. A corporate
GitLab mirror (`gitlab.keeneticcorp.ru`) carries the same `main` and tags for
network-restricted environments; releases are cut on `main` and pushed to both.
CI lives in GitHub Actions only.

## Development

```powershell
poetry install
poetry run ruff check src tests
poetry run ruff format --check src tests
poetry run mypy src
poetry run pytest
poetry build
```

The package tests itself with `tests/test_devloop.py`; the CI matrix covers
Ubuntu and Windows on Python 3.11 and 3.13, and builds the wheel on `v*` tags.

## Documentation

* [Integration](docs/integration.md) — installing into a project, the full
  configuration reference and the adapter contracts.
* [Design](docs/design.md) — why static selection, why the selector is a command,
  what was evaluated and rejected.
