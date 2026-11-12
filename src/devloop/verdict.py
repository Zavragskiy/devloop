"""A green verdict cache keyed by the exact tree state.

``check --full`` is the expensive step: every static check plus the whole test
suite. Running it twice on the same tree cannot find anything new, so the tree
key of the last green full run is recorded and a matching key lets the next run
short-circuit in about a second. The key covers the commit, the staged and
unstaged diff, and the content of every untracked file, so any edit, stage or
revert invalidates it; ignored paths (a cache directory under ``.gitignore``) do
not.
"""

from __future__ import annotations

import json

from devloop import gitinfo
from devloop.cache import Cache, digest, hash_file
from devloop.config import Config

VERDICT_FILE = "verify-full.json"
GREEN = "green"


def _cache(cfg: Config) -> Cache:
    return Cache(cfg.cache_dir / VERDICT_FILE)


def tree_key(cfg: Config) -> str:
    """A content hash of the commit plus the working tree."""
    untracked = {
        change.path: hash_file(cfg.root / change.path)
        for change in gitinfo.working_tree(cfg.root)
        if change.untracked
    }
    payload = {
        "head": gitinfo.output(cfg.root, "rev-parse", "HEAD").strip(),
        "status": gitinfo.output(cfg.root, "status", "--porcelain=v1", "-z"),
        "diff": gitinfo.output(cfg.root, "diff", "HEAD", "--binary", "--no-color"),
        "untracked": untracked,
    }
    return digest(json.dumps(payload, sort_keys=True).encode("utf-8"))


def recent(cfg: Config, key: str) -> bool:
    """True when this exact tree already passed the full gate."""
    recorded = _cache(cfg).get("last") or {}
    return recorded.get("key") == key and recorded.get("verdict") == GREEN


def remember(cfg: Config, key: str) -> None:
    """Record that this tree passed the full gate."""
    cache = _cache(cfg)
    cache.put("last", {"key": key, "verdict": GREEN})
    cache.save()
