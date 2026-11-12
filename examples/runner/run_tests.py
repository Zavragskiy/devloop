#!/usr/bin/env python3
"""A sharded pytest runner for devloop that works on Windows.

Copy this file into the consuming project (for example ``tools/run_tests.py``),
point ``runner`` at it and devloop will call it with the contract that
``docs/integration.md`` describes::

    python tools/run_tests.py --files tests/a.py tests/b.py --jobs 4
    python tools/run_tests.py --only ui/
    python tools/run_tests.py --last-failed

Each test file becomes one shard. The runner records the outcome in
``<cache_dir>/run-tests.json`` and leaves the captured output of a failing shard
in ``<cache_dir>/shard-<name>.log``, so ``devloop check`` prints the failing ids
and the traceback tail without a second command.

It only uses the standard library plus ``devloop.config``; pytest comes from the
project's own environment through ``sys.executable``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from devloop import config, process

FAILED_LINE = re.compile(r"^FAILED (\S+)", re.MULTILINE)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_tests.py", description=__doc__.splitlines()[0])
    parser.add_argument("--files", nargs="+", default=[], metavar="PATH", help="test files to run")
    parser.add_argument(
        "--only", action="append", default=[], metavar="TOKEN", help="keep paths containing TOKEN"
    )
    parser.add_argument("--last-failed", action="store_true", help="replay the last failures")
    parser.add_argument("--jobs", type=int, default=0, help="shards to run in parallel")
    return parser.parse_args(argv)


def discover(cfg: config.Config) -> list[str]:
    """Every test file under ``tests_root``, repository-relative and sorted."""
    root = cfg.root / cfg.tests_root
    if not root.is_dir():
        return []
    paths = set(root.rglob("test_*.py")) | set(root.rglob("*_test.py"))
    return sorted(cfg.relative(path) for path in paths)


def slug(name: str) -> str:
    """A file name that is legal on Windows for a shard called ``name``."""
    clean = re.sub(r"[^A-Za-z0-9_.-]", "-", name.replace("\\", "/").replace("/", "-"))
    return clean.strip("-") or "shard"


def last_failed(cfg: config.Config) -> list[str]:
    """The failing test ids of the previous run, or an empty list."""
    try:
        payload = json.loads((cfg.cache_dir / "run-tests.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids: list[str] = []
    if isinstance(payload, dict):
        for shard in payload.get("shards", []):
            if isinstance(shard, dict):
                ids.extend(str(item) for item in shard.get("failed_tests", []))
    return ids


def plan(cfg: config.Config, args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    """One ``(shard name, pytest targets)`` pair per test file, or one replay shard."""
    if args.last_failed:
        ids = last_failed(cfg)
        return [("last-failed", ids)] if ids else []
    files = list(args.files) or discover(cfg)
    for token in args.only:
        files = [path for path in files if token in path]
    return [(Path(path).name, [path]) for path in files]


def run_shard(cfg: config.Config, name: str, targets: list[str]) -> dict[str, object]:
    """Run one pytest shard and record its ids, failures and log path."""
    command = [sys.executable, "-m", "pytest", "-q", *targets]
    env = dict(os.environ)
    env.update(cfg.environment)
    result = process.run(command, cwd=cfg.root, env=env)
    output = result.stdout + result.stderr
    log = f"shard-{slug(name)}.log"
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir / log).write_text(output, encoding="utf-8")
    failed = list(dict.fromkeys(FAILED_LINE.findall(output)))
    return {
        "name": name,
        "failed": len(failed),
        "failed_tests": failed,
        "log": (cfg.cache_dir / log).relative_to(cfg.root).as_posix(),
        "code": result.returncode,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = config.load()
    shards = plan(cfg, args)
    if not shards:
        print("run_tests.py: nothing to run")
        return 0
    jobs = max(1, args.jobs)
    if jobs == 1:
        results = [run_shard(cfg, name, targets) for name, targets in shards]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda item: run_shard(cfg, *item), shards))
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir / "run-tests.json").write_text(
        json.dumps({"shards": results}, indent=2), encoding="utf-8"
    )
    failed = sum(1 for result in results if result["code"] != 0)
    print(f"run_tests.py: {len(results)} shard(s), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
