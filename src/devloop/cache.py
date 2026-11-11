"""A tiny JSON cache keyed by content hashes.

The cache is an optimisation only: a miss or an unreadable file must never
change the result, so every read is guarded and a corrupt cache is ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def digest(data: bytes) -> str:
    """The content hash used by every cache key."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    """The SHA-256 of a file, or an empty string when it cannot be read."""
    try:
        return digest(path.read_bytes())
    except OSError:
        return ""


def hash_paths(root: Path, paths: list[Path]) -> dict[str, str]:
    """Map repo-relative POSIX paths to their content hashes."""
    hashes: dict[str, str] = {}
    for path in paths:
        try:
            key = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            key = path.as_posix()
        hashes[key] = hash_file(path)
    return hashes


class Cache:
    """One JSON object on disk; callers namespace their own keys."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = self._load()

    def _load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def put(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        """Write atomically; a failed write leaves the old file in place."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
