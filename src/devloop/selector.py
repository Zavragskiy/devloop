"""Select affected tests by a cached static import graph."""

from __future__ import annotations

from pathlib import Path

from devloop import gitinfo, graph
from devloop.config import Config


def _seeds(cfg: Config, changes: list[gitinfo.Change], graph_: graph.ImportGraph) -> set[str]:
    """Modules a change touches; a package ``__init__`` covers its whole package."""
    seeds: set[str] = set()
    for change in changes:
        seeds |= _change_seeds(cfg, change, graph_)
    return seeds


def _change_seeds(cfg: Config, change: gitinfo.Change, graph_: graph.ImportGraph) -> set[str]:
    path = _changed_python(cfg, change)
    if path is None:
        return set()
    if change.path.startswith(cfg.tests_root.rstrip("/") + "/"):
        name = graph.module_name(path, [cfg.root])
        return {name} if name else set()
    name = graph.source_module(cfg, path)
    if name is None:
        return set()
    if path.name == "__init__.py":
        return _package_modules(graph_, name)
    return {name}


def _changed_python(cfg: Config, change: gitinfo.Change) -> Path | None:
    if change.deleted or not change.path.endswith(".py"):
        return None
    path = cfg.root / change.path
    return path if path.is_file() else None


def _package_modules(graph_: graph.ImportGraph, name: str) -> set[str]:
    return {module for module in graph_.modules if module == name or module.startswith(name + ".")}


def select(cfg: Config, changes: list[gitinfo.Change]) -> list[str]:
    """Return tests reached from changed modules; raise on uncertain parsing."""
    graph_ = graph.build(cfg)
    if graph_.unparsed:
        raise ValueError("unparseable files: " + ", ".join(sorted(graph_.unparsed)))
    seeds = _seeds(cfg, changes, graph_)
    if not seeds:
        return []
    return sorted(graph_.tests[node] for node in graph_.dependents(seeds))
