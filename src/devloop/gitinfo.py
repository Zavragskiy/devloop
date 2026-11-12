"""Changed files from git, including untracked ones.

A verification loop must see exactly what a commit would see: staged and
unstaged edits plus new files. Staged renames report the new path and a
synthetic deletion for the old path, so test impact cannot miss moved modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from devloop import process


@dataclass(frozen=True)
class Change:
    """One working-tree entry: ``status`` follows ``git status --porcelain``."""

    path: str
    status: str

    @property
    def deleted(self) -> bool:
        return "D" in self.status

    @property
    def untracked(self) -> bool:
        return self.status == "??"


def _run(root: Path, *args: str) -> process.Result:
    """Run one git command in ``root`` without raising on a non-zero exit."""
    return process.run(["git", *args], cwd=root)


def output(root: Path, *args: str) -> str:
    """Output of a read-only git query, empty when the command fails.

    The verdict key is only a cache: a repo without history, a detached state
    or a missing git executable must degrade to a cache miss, never raise.
    """
    return _run(root, *args).stdout


def _git(root: Path, *args: str) -> str:
    result = _run(root, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def working_tree(root: Path) -> list[Change]:
    """Every staged, unstaged and untracked path relative to ``root``."""
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = status.split("\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        change_status = entry[:2]
        path = entry[3:]
        if change_status[0] in "RC" or change_status[1] in "RC":
            old_path = fields[index]
            index += 1
            if "R" in change_status and old_path != path:
                changes.append(Change(path=old_path, status=" D"))
        changes.append(Change(path=path, status=change_status))
    return changes


def since(root: Path, base: str) -> list[Change]:
    """Committed, staged and unstaged changes against ``base``, plus untracked."""
    listed = _git(root, "diff", "--name-status", "--no-renames", "-z", f"{base}...HEAD")
    fields = listed.split("\0")
    changes: dict[str, Change] = {}
    for status, path in zip(fields[::2], fields[1::2]):
        if path:
            changes[path] = Change(
                path=path, status=" D" if status.startswith("D") else status[:1] + " "
            )
    for change in working_tree(root):
        changes[change.path] = change
    return list(changes.values())


def collect(root: Path, base: str | None = None) -> list[Change]:
    """The change set for a run: working tree by default, ``base`` optionally."""
    return since(root, base) if base else working_tree(root)
