# Integrating devloop

Run all commands from the consuming project with its own Poetry (or pip)
environment. The package is installed into that environment and reads **that
project's** `pyproject.toml`. It locates the nearest ancestor containing
`[tool.devloop]`, so commands also work from a project subdirectory. A Git
checkout and Python 3.11 or newer are required.

## Install and pin

Local evaluation, from a checkout:

```powershell
poetry add --group dev --editable D:/Projects/devloop
poetry add --group dev ruff mypy pytest
```

Team and CI, pinned to an immutable commit (GitHub is the primary home):

```toml
[tool.poetry.group.dev.dependencies]
devloop = { git = "https://github.com/Zavragskiy/devloop.git", rev = "<commit-sha>" }
ruff = "^0.15"
mypy = "^1.11"
pytest = "^9.0"
```

A network-restricted environment can use the corporate GitLab mirror instead:

```toml
devloop = { git = "ssh://git@gitlab.keeneticcorp.ru/development/applications/devloop.git", rev = "<commit-sha>" }
```

Run `poetry lock`, commit the lockfile in the consuming project, and use
`poetry install --with dev` in CI. A local path dependency is useful during
development but cannot resolve on another machine unless the same path exists.

For a project using pip instead of Poetry:

```powershell
pip install "git+https://github.com/Zavragskiy/devloop.git@<commit-sha>"
pip install ruff mypy pytest
devloop --help
```

The consuming project must provide Ruff, mypy and pytest. No code is copied into
the consuming project, and nothing in `devloop` imports project code.

## Minimal configuration

```toml
[tool.devloop]
source_roots = ["src/my_package"]  # required: paths relative to the project root
tests_root = "tests"
cache_dir = ".devloop"
shared = ["pyproject.toml", "poetry.lock"]
```

Ignore the cache directory in the consuming project's `.gitignore`.
`source_roots` lists the Python directories or files to analyze; the built-in
selector builds an import graph from them and `tests_root`. The default test
runner is `<project python> -m pytest -q <selected files>`.

## Adapters

Every project-specific step is a command, so an existing gate script or sharded
runner keeps working unchanged.

### Selector

`selector` prints affected test paths, one existing file per stdout line,
relative or absolute. The default is the built-in static selector:

```toml
selector = ["{python}", "-m", "devloop", "select-tests"]
```

`{python}` expands to the consuming environment's interpreter. `devloop` appends
`--base <ref>` when `--base` is given. A non-zero exit, an empty selection or a
line that is not an existing test file all fail open to the full suite. Any
command works:

```toml
selector = ["{python}", "tools/select_tests.py"]
```

### Quality gate

`quality` is called as `<project python> <quality> <Python file paths...>` with
the changed files, or every source and test file on `--full`. It must return a
nonzero exit code on failure. Omit it (or set it empty) to skip the step.

```toml
quality = "tools/check_quality.py"
```

A baseline can freeze what the script reports today. To take part, the script
reads `DEVLOOP_FINDINGS_FILE` from the environment and appends one JSON object
per finding:

```json
{"key": "duplicate-main@build_modules/z.py", "path": "build_modules/z.py", "message": "two main() definitions"}
```

Only `key` is required. A script that ignores the variable keeps working exactly
as before; its findings are simply not covered by the baseline. When the
variable is set and the script exits nonzero, emitting no key at all is treated
as a broken script, not as a clean run. The run prints the keys only, since the
script already printed its own detail; the built-in tools keep their message
because their output is captured. See [Baseline](#baseline).

A warning for adapters built on `lizard`: `lizard -w --csv` prints human
`warning:` lines instead of CSV in some versions, so a parser can silently see
zero findings and leave the gate half-blind. Check the output shape before
trusting it.

### Policy gate

`policy` is called as `<project python> <policy>` with **no paths**: it covers
the whole tree (layout, documentation, package shape). It runs on every
`check`, including `--full`, and must return a nonzero exit code on failure.

```toml
policy = "tools/check_package_policy.py"
```

### Test runner

`runner` is called as `<project python> <runner> --files <test file paths...>`,
plus `--jobs N` when `--jobs` was passed. An explicit selection replaces
`--files` with the forwarded flags: `--only <token>` (repeated) and/or
`--last-failed`. It must return a nonzero exit code when tests fail. Omit it for
direct pytest execution.

```toml
runner = "tools/run_tests.py"
```

Omit `runner` (or set it empty) to run pytest directly. The direct mode maps the
same request onto pytest: the selected files become positional arguments,
`--only <token>` keeps the test files whose path contains the token (an error if
none matches, never a surprise full run), `--last-failed` becomes pytest's own
`--lf`, and `--jobs` is ignored because it only applies to a sharding runner.

Optionally the runner records its outcome so a failing run explains itself. Write
`<cache_dir>/run-tests.json`:

```json
{
  "shards": [
    {
      "name": "test_x.py",
      "failed": 1,
      "failed_tests": ["tests/x.py::T::a"],
      "log": ".devloop/shard-test_x.py.log"
    }
  ]
}
```

and leave the captured output of each failing shard in the file named by `log`.
When the test step fails, `check` prints `failing: <ids>` and the last 20 lines of
each log. The same records keep `--last-failed` working across runs. A runner
that writes nothing is fine; the failure output is then just the exit code.

### Runner recipes

`examples/runner/run_tests.py` in this repository is a complete sharded runner
for Windows: one pytest subprocess per test file, `--jobs N` in parallel through
`concurrent.futures`, no third-party dependency. Copy it into the project as
`tools/run_tests.py`, point `runner` at it, and the contract becomes:

| Flag | What the runner does |
| --- | --- |
| `--files PATH...` | one shard per test file, each run as `python -m pytest -q <file>` |
| `--only TOKEN...` | keep the test files whose path contains a token |
| `--last-failed` | replay the `failed_tests` recorded by the previous run |
| `--jobs N` | up to N shards in parallel |

It writes `<cache_dir>/run-tests.json` and `<cache_dir>/shard-<name>.log`, so the
failure output above works without any extra wiring.

### Extra checks

`[[tool.devloop.check]]` runs one more project command as a pipeline step, after
mypy and before the tests. This is how an acceptance shard or an existing
plan-run step joins the same pipeline instead of being a second definition of
"clean":

```toml
[[tool.devloop.check]]
name = "plan-run"
command = ["{python}", "scripts/plan_run.py", "--shard", "acceptance"]
paths = "none"          # none | changed | all
```

`paths` decides the arguments: `none` appends nothing (like `policy`), `changed`
appends the changed Python files, `all` appends every source and test file.
`changed` is **not** widened by `--full`: it still receives only the changed
files, and on a clean tree the step is skipped with a line saying so. A check
that must cover the whole tree in the commit gate uses `all`; `changed` is for
the edit loop. A nonzero exit fails the step and the pipeline. With a baseline in
play the step also receives `DEVLOOP_FINDINGS_FILE`, and its keys land under
`name` in the baseline file. Names must be unique; the order of the tables is
the order the checks run in.

## Baseline

A project with legacy debt can turn its gates on today and fail only on what a
change adds. `devloop baseline` runs the finding steps over the whole tree
(ruff, mypy, `quality` and every `[[tool.devloop.check]]`; the tests and `policy`
are not finding steps) and writes `devloop-baseline.json`:

```json
{
  "version": 1,
  "generated_by": "devloop 0.3.1",
  "entries": {
    "ruff": ["build_modules/x.py:E501", "build_modules/x.py:F401"],
    "format": ["build_modules/legacy_view.py:format"],
    "mypy": ["shared_utils/y.py:attr-defined"],
    "quality": ["duplicate-main@build_modules/z.py"]
  }
}
```

Commit the file. After that, `devloop check` fails only on a key that is not in
it, and prints the key and the tool's own message. Ruff is run as
`ruff check --output-format json --exit-zero`, mypy with
`--show-error-codes --no-error-summary`; the keys are `<path>:<code>`, so the
ratchet is per file and per code, the way an inline `# noqa` is. The same key
twice in one file is one entry.

The formatter is a finding step too: `ruff format --check` prints
`Would reformat: <path>` and the key is `<path>:format`, so a tree full of
unformatted legacy files can be gated without reformatting it in one commit.

Keys written by a project command are free-form, but keep them readable across
repositories: the built-in tools already use `:` between the path and the code,
so `<path>::<symbol>:<rule>` (for example
`pkg/x.py::build_panel:complexity`) avoids an ambiguous key.

The semantics that matter:

- a key in the baseline passes, whatever its count;
- a key that is not in the baseline fails the step, and the run names it;
- entries that are no longer reported are **stale**; that is printed only on
  `--full` (a changed-file run sees a subset of the tree, so it must not call the
  rest stale) and never fails the run; `devloop baseline` rewrites the file;
- `devloop baseline --check` runs the same collection without writing and exits
  `1` when anything is new or stale, so CI can keep the accepted debt from
  rotting: every fix has to delete its entry;
- no baseline file means the mechanism is off; a corrupt or wrong-version file is
  an error, never a silently empty baseline;
- `baseline = ""` disables the mechanism even when a file exists.

`devloop baseline` refuses to write a partial file: if a tool fails and prints
nothing parseable, the command exits `2` and leaves the old baseline in place.

## Configuration reference

| Key | Default | Meaning |
| --- | --- | --- |
| `source_roots` | required | Source directories or files, relative to the project root. |
| `tests_root` | `tests` | Test discovery root. |
| `cache_dir` | `.devloop` | Import-graph cache, verdict record and runner summary. |
| `quality` | disabled | Optional gate script receiving the selected Python files. |
| `policy` | disabled | Optional whole-tree gate script receiving no paths. |
| `runner` | direct pytest | Optional runner script receiving `--files`/`--only`/`--last-failed`/`--jobs`. |
| `selector` | built-in static selector | Command printing affected test paths. |
| `shared` | `pyproject.toml` | Files whose change triggers the full suite. |
| `always` | empty | Test files included in every narrow selection. |
| `max_fraction` | `0.6` | A selection at or above this fraction runs all tests. |
| `baseline` | `devloop-baseline.json` | Committed finding ratchet; `""` disables it. |
| `env` | empty | Extra variables for every step subprocess; `{root}` expands. |
| `mypy.scope` | empty | Paths passed to mypy; empty keeps the project's own mypy config. |
| `mypy.changed_only` | `false` | mypy checks only the changed files inside the scope. |
| `check` | empty | Extra `[[tool.devloop.check]]` steps, run in declaration order. |

`devloop --help` prints the same switches as the installed version.

## Environment

Ruff and mypy must exist in the consuming environment, or their steps fail.
mypy is invoked without file arguments by default, so the project's own mypy
configuration (scope, overrides) decides what is checked.

`[tool.devloop.env]` adds variables to every step subprocess — ruff, format,
mypy, `quality`, `policy`, `runner`, the selector and every
`[[tool.devloop.check]]` — without touching devloop's own environment. This is
where the GUI settings belong:

```toml
[tool.devloop.env]
QT_QPA_PLATFORM = "offscreen"
QT_QPA_FONTDIR = "{root}/fonts"
PYTHONPATH = "{root}/src"
```

Values are strings, `{root}` expands to the project root, and nothing goes
through a shell.

`[tool.devloop.mypy]` moves the scope from the project's `[tool.mypy].files` into
the pipeline, so it can grow package by package:

```toml
[tool.devloop.mypy]
scope = ["shared_utils", "support_modules", "build_modules"]
changed_only = true
```

With `changed_only`, or the `--changed-only` flag, the changed mode passes the
changed files inside the scope instead of the whole scope. On `--full` the whole
scope is always checked, because a full run is the final gate and a clean tree
with `changed_only` would otherwise be a green run that checked nothing. The
flag is ignored there with a line saying so. Fields listed here are validated at
load time: a scope entry must exist inside the project.

`devloop.cache` (`Cache`, `digest`, `hash_file`) is documented public API, so a
project gate can reuse the same content-hash cache instead of inventing one.

## Developer and CI workflow

```powershell
poetry run devloop check --explain
poetry run devloop impact
poetry run devloop check --base origin/main
poetry run devloop check --full
poetry run devloop baseline          # after accepting today's debt
```

In CI, fetch the base ref before using `--base`; otherwise Git cannot compute the
comparison. Static selection is conservative but cannot identify every dynamic
import, so a full run remains necessary before merging. The verdict cache also
makes it cheap: an unchanged tree answers in about a second.

## Migrating from an in-repo copy

A project that previously vendored the pipeline (for example `tools/devloop/`)
can drop it after installing the package:

1. install `devloop` as a development dependency and confirm `devloop --help`;
2. delete the vendored package and any selector script that the built-in
   `select-tests` replaces;
3. drop the `selector` key if the built-in selector is what the project used;
4. keep the project's own gate scripts — they are the `quality`, `policy` and
   `runner` adapters;
5. move the pipeline's tests into this repository (or delete them) and keep the
   tests of the project's own gates in the project.
