"""What a failed test step prints: the failing ids and the shard log tails.

A custom runner may record its shard outcome in ``<cache_dir>/run-tests.json``
and leave the captured output of a failing shard in
``<cache_dir>/shard-<name>.log``. Reading that back is what turns
``[verify] tests: FAILED`` into the name of the test and its traceback without a
second command. The summary is optional: a runner that writes nothing simply
produces no extra output.
"""

from __future__ import annotations

import json
from typing import Any

from devloop.config import Config

SUMMARY_FILE = "run-tests.json"
LOG_TAIL = 20
"""Lines of a failing shard log printed when a test step fails."""


def summary(cfg: Config) -> dict[str, Any]:
    """The runner summary of the last run, or an empty dict."""
    try:
        payload = json.loads((cfg.cache_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def test_failures(cfg: Config) -> None:
    """Name the failing tests and show the tail of each failing shard log."""
    shards = [
        shard
        for shard in summary(cfg).get("shards", [])
        if shard.get("failed_tests") or shard.get("log")
    ]
    names = [test for shard in shards for test in shard.get("failed_tests", [])]
    if names:
        print("[verify] failing: " + ", ".join(names))
    for shard in shards:
        _log_tail(cfg, shard.get("log", ""))


def _log_tail(cfg: Config, log: str) -> None:
    if not log:
        return
    try:
        lines = (cfg.root / log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    print(f"[verify] {log}, last {min(len(lines), LOG_TAIL)} of {len(lines)} lines:")
    for line in lines[-LOG_TAIL:]:
        print(f"  {line}")
