"""The baseline file: the findings a tree already has, frozen.

A baseline turns a gate that is red on legacy code into a gate that is red only
on what the change added. It is one committed JSON file, one sorted list of keys
per step, so a review of the baseline is a review of accepted debt.

``stale`` entries (frozen but no longer reported) are only looked for on a full
run: a changed-file run sees a subset of the tree, and calling everything else
stale is exactly the trap that makes a baseline unusable from a pre-commit hook.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from devloop.findings import Finding

VERSION = 1


@dataclass(frozen=True)
class Comparison:
    """New findings, stale entries and how many findings matched the baseline."""

    new: dict[str, tuple[Finding, ...]]
    stale: dict[str, tuple[str, ...]]
    known: int

    @property
    def has_new(self) -> bool:
        return any(self.new.values())


def load(path: Path) -> dict[str, set[str]]:
    """Read a baseline; a missing file is an error the caller turns into "off"."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read the baseline {path}: {error}") from error
    except ValueError as error:
        raise ValueError(f"the baseline {path} is not JSON: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        raise ValueError(f"the baseline {path} must say version {VERSION}")
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise ValueError(f"the baseline {path} has no entries table")
    result: dict[str, set[str]] = {}
    for step, keys in entries.items():
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError(f"the baseline entry {step} must be a list of strings")
        result[str(step)] = set(keys)
    return result


def save(path: Path, entries: Mapping[str, Iterable[str]]) -> None:
    """Write the baseline atomically, sorted, and drop the empty steps."""
    payload = {
        "version": VERSION,
        "entries": {step: sorted(set(keys)) for step, keys in sorted(entries.items()) if set(keys)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def compare(
    entries: Mapping[str, set[str]],
    observed: Mapping[str, list[Finding]],
    steps: Iterable[str],
    full: bool,
) -> Comparison:
    """Split the observations of this run into new findings and stale entries."""
    new: dict[str, tuple[Finding, ...]] = {}
    known = 0
    for step in sorted(set(steps)):
        frozen = entries.get(step, set())
        fresh: dict[str, Finding] = {}
        seen = 0
        for finding in observed.get(step, []):
            if finding.key in frozen:
                seen += 1
            else:
                fresh.setdefault(finding.key, finding)
        new[step] = tuple(fresh.values())
        known += seen
    stale: dict[str, tuple[str, ...]] = {}
    if full:
        for step in sorted(entries):
            if step not in new:
                continue
            missing = entries[step] - {finding.key for finding in observed.get(step, [])}
            if missing:
                stale[step] = tuple(sorted(missing))
    return Comparison(new=new, stale=stale, known=known)
