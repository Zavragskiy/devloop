"""Machine-readable findings from the steps a baseline can freeze.

The baseline is a ratchet over what a tool reports, so the report has to be
stable enough to compare across runs. Ruff is asked for JSON, mypy is parsed for
``error`` lines with their code, and the project's own commands (``quality`` and
``[[tool.devloop.check]]``) write one JSON object per finding to the file named
by ``DEVLOOP_FINDINGS_FILE``.

A key is ``<path>:<code>`` for the built-in tools: the code is the tool's own
identifier, the path the repository-relative POSIX path. The same finding twice
in one file collapses into one key, so the baseline ratchets per file and code,
the way an inline ``# noqa`` does. A command that cannot say what it found is
not guessed at: parsing raises instead of returning a clean result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MYPY_TAIL = re.compile(r"^(?P<body>.*?)\s+\[(?P<code>[A-Za-z0-9_-]+)\]$")
MYPY_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): error: (?P<message>.*)$")


@dataclass(frozen=True)
class Finding:
    """One reportable finding; ``message`` is ready to print."""

    step: str
    key: str
    message: str


def _relative(root: Path, raw: str) -> str:
    """A tool's path as a repository-relative POSIX path."""
    text = raw.strip().replace("\\", "/")
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return text[2:] if text.startswith("./") else text


def _payload(output: str) -> list[object]:
    """The JSON array in the output, ignoring anything before it."""
    text = output.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < start:
            raise ValueError("the tool did not print JSON") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except ValueError as error:
            raise ValueError(f"the tool JSON is malformed: {error}") from error
    if not isinstance(parsed, list):
        raise ValueError("the tool JSON is not a list")
    return parsed


def parse_ruff(output: str, root: Path, step: str = "ruff") -> list[Finding]:
    """``ruff check --output-format json`` as findings keyed by path and code."""
    found: list[Finding] = []
    for item in _payload(output):
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "misc")
        path = _relative(root, str(item.get("filename", "")))
        location = item.get("location")
        row = location.get("row", 0) if isinstance(location, dict) else 0
        message = str(item.get("message", "")).strip()
        found.append(Finding(step, f"{path}:{code}", f"{path}:{row}: {code} {message}"))
    return found


def parse_mypy(output: str, root: Path, step: str = "mypy") -> list[Finding]:
    """``mypy`` error lines as findings keyed by path and error code."""
    found: list[Finding] = []
    for raw in output.splitlines():
        line = raw.strip()
        tail = MYPY_TAIL.match(line)
        code = "misc"
        if tail is not None:
            line = tail.group("body")
            code = tail.group("code")
        match = MYPY_LINE.match(line)
        if match is None:
            continue
        path = _relative(root, match.group("path"))
        found.append(Finding(step, f"{path}:{code}", raw.strip()))
    return found


def read_report(path: Path, step: str) -> list[Finding]:
    """The JSONL a step wrote to ``DEVLOOP_FINDINGS_FILE``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    found: list[Finding] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError as error:
            raise ValueError(f"{step}: findings line {number} is not JSON: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"{step}: findings line {number} is not an object")
        key = payload.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{step}: findings line {number} has no key")
        message = payload.get("message")
        found.append(Finding(step, key.strip(), str(message) if message else key.strip()))
    return found
