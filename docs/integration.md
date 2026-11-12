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

`devloop --help` prints the same switches as the installed version.

## Environment

Ruff and mypy must exist in the consuming environment, or their steps fail.
mypy is invoked without file arguments, so the project's own mypy configuration
(scope, overrides) decides what is checked. `devloop.cache`
(`Cache`, `digest`, `hash_file`) is documented public API, so a project gate can
reuse the same content-hash cache instead of inventing one.

## Developer and CI workflow

```powershell
poetry run devloop check --explain
poetry run devloop impact
poetry run devloop check --base origin/main
poetry run devloop check --full
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
