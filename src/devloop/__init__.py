"""Fast, configurable verification for Python projects.

Modules:

* :mod:`devloop.config` - the ``[tool.devloop]`` table and its defaults.
* :mod:`devloop.gitinfo` - which files changed, including untracked ones.
* :mod:`devloop.impact` - the affected-test selection and its fail-open rules.
* :mod:`devloop.graph` - the static import graph the selector walks.
* :mod:`devloop.cache` - a small JSON cache keyed by content hashes.
* :mod:`devloop.process` - run a child and decode its output without a Unicode surprise.
* :mod:`devloop.findings` - tool output as the keys a baseline freezes.
* :mod:`devloop.baseline` - the baseline file and its comparison.
* :mod:`devloop.report` - what a failed test step prints.
* :mod:`devloop.verdict` - the green full-run cache keyed by tree state.
* :mod:`devloop.cli` - the ``check`` / ``baseline`` / ``impact`` / ``changed`` commands.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.2"
