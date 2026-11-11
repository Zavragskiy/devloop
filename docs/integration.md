# Integrating devloop

Run all commands from the consuming project with its own Poetry environment.
The package is installed into that environment and reads **that project's**
`pyproject.toml`. It locates the nearest ancestor containing
`[tool.devloop]`, so commands also work from a project subdirectory. A Git
checkout and Python 3.11 or newer are required.

## Install and pin

For a local evaluation:

```powershell
poetry add --group dev D:/Projects/devloop
poetry add --group dev ruff mypy pytest
```

For a team or CI deployment, pin an immutable commit from the private
GitLab repository in the consuming project's `pyproject.toml`:

```toml
[tool.poetry.group.dev.dependencies]
devloop = { git = "ssh://git@gitlab.keeneticcorp.ru/development/applications/devloop.git", rev = "<commit-sha>" }
ruff = "^0.15"
mypy = "^1.11"
pytest = "^9.0"
```

Replace `<commit-sha>` with the commit to deploy. Configure SSH access
to the private repository for developers and CI. Run
`poetry lock`, commit the lockfile in the consuming project, and use
`poetry install --with dev` in CI. A local path dependency is useful during
development but cannot resolve on another machine unless the same path exists.

For a project using pip instead of Poetry, install this repository alongside
the project dependencies in the same virtual environment and invoke
`devloop`. The consuming project must provide Ruff, mypy, and pytest.

## Minimal configuration

```toml
[tool.devloop]
source_roots = ["src/my_package"]  # required: paths relative to the Git root
tests_root = "tests"
cache_dir = ".cache/devloop"
shared = ["pyproject.toml", "poetry.lock"]
```

Ignore the cache directory in the consuming project's `.gitignore`.
`source_roots` must list the Python source directories or files to analyze. The
built-in selector builds an import graph from these paths and `tests_root`.
The default test runner is `python -m pytest -q <selected files>`.

## Custom adapters

To keep an existing project-specific quality gate or sharded test runner:

```toml
[tool.devloop]
source_roots = ["my_package"]
tests_root = "tests"
cache_dir = "tmp/devloop"
quality = "tools/check_quality.py"
runner = "tools/run_tests.py"
shared = ["pyproject.toml", "poetry.lock"]
always = ["tests/test_package_boundaries.py"]
max_fraction = 0.6
```

The quality script is called as
`<project-python> tools/check_quality.py <Python file paths...>`.
It must return a nonzero exit code on failure. Omit `quality` or set it to
an empty string to skip this optional step.

The runner is called as
`<project-python> tools/run_tests.py --files <test file paths...>`.
When `--jobs N` is passed to devloop, the custom runner also receives
`--jobs N`. It must return a nonzero exit code when tests fail. Omit
`runner` or set it to an empty string for direct pytest execution.

The built-in selector is equivalent to:

```toml
selector = ["{python}", "-m", "devloop", "select-tests"]
```

You can replace it with any command that prints one existing test path per
stdout line, relative or absolute. `{python}` expands to the consuming
environment's Python executable. For example:

```toml
selector = ["{python}", "tools/select_tests.py"]
```

Devloop appends `--base <ref>` when given `--base`. Return a nonzero code
if selection is uncertain; devloop then runs the full test suite. An empty or
invalid selection also runs the full suite.

## Configuration reference

| Key | Default | Meaning |
| --- | --- | --- |
| `source_roots` | required | Source directories or files, relative to project root. |
| `tests_root` | `tests` | Test discovery root. |
| `cache_dir` | `.devloop` | Static graph cache directory. |
| `quality` | disabled | Optional Python gate script receiving selected files. |
| `runner` | direct pytest | Optional Python script receiving `--files` and `--jobs`. |
| `selector` | built-in static selector | Command printing affected test paths. |
| `shared` | `pyproject.toml` | Files whose changes trigger the full suite. |
| `always` | empty | Test files included in every narrow selection. |
| `max_fraction` | `0.6` | Selection at or above this fraction runs all tests. |

Check the installed version's `devloop --help` and package source for exact
defaults if upgrading.

## Developer and CI workflow

```powershell
poetry run devloop check --explain
poetry run devloop impact
poetry run devloop check --base origin/main
poetry run devloop check --full
```

For a local edit loop, use `check`. Run `check --full` at the stage or
commit gate. In CI, fetch the base ref before using `--base`; otherwise Git
cannot compute the comparison. Static selection is conservative but cannot
identify every dynamic import, so a full run remains necessary before merging.

If Ruff or mypy are absent from the consuming environment, their corresponding
check fails. Install them as development dependencies. A project can put
additional mypy settings in its own `pyproject.toml`.






