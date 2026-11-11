# devloop

A fast verification loop for Python projects. `devloop check` finds changed files,
runs Ruff, an optional project quality gate, mypy for source changes, and the tests
affected by the change. `devloop check --full` covers every source and test file and
runs the complete test suite.

The installed package uses only the Python standard library (Python 3.11+).
Ruff, mypy, pytest, and any custom quality gate are supplied by the **project
being checked**. The default test selector analyzes Python imports statically; it
does not require a previous coverage run.

## Install in a Poetry project

Add this repository as a development dependency from a local checkout:

```powershell
poetry add --group dev D:/Projects/devloop
poetry add --group dev ruff mypy pytest
poetry run devloop --help
```

For team and CI installation, use the private GitLab repository and pin a
commit instead of a local path. The exact dependency syntax is documented in
[Integration](docs/integration.md). Keep `devloop` in the development dependency
group so it is available to developers and CI without becoming an application
runtime dependency. No code needs to be copied into the consuming project.

## Configure the consuming project

Add this to its `pyproject.toml`. `source_roots` is required and lists source paths:

```toml
[tool.devloop]
source_roots = ["my_package"]
tests_root = "tests"
cache_dir = ".cache/devloop"
shared = ["pyproject.toml", "poetry.lock"]
```

The default selector is `python -m devloop select-tests`; the default runner is
`python -m pytest -q <selected files>`. The runner must be installed in the
consuming project's environment. See [Integration](docs/integration.md) for
custom selectors, quality gates, test runners, deployment in CI, and the full
configuration reference.

## Run

From the consuming project's root:

```powershell
poetry run devloop check
poetry run devloop check --explain
poetry run devloop impact
poetry run devloop changed
poetry run devloop check --base origin/main
poetry run devloop check --full
```

`check` is the default command, so `poetry run devloop` also works.
`--no-tests` skips test execution; `--keep-going` runs later checks after a
failure. `--jobs N` is passed only to a configured custom runner.

## Selection safety

The selector follows imports and literal `patch(...)` and
`import_module(...)` targets. It includes tests that depend on changed modules,
directly changed test files, and any configured `always` tests. When it cannot
make a reliable narrow choice, it selects the full suite: for example after a
selector failure, a changed `conftest.py`, a shared input or source asset
change, an unparseable source file, or a selection above `max_fraction`.
Run `check --full` before a commit or release to cover dynamic dependencies
that static analysis cannot discover.

## Development

```powershell
poetry install
poetry run pytest -q
poetry run ruff check src tests
poetry build
```

Repository: [private GitLab](https://gitlab.keeneticcorp.ru/development/applications/devloop).




