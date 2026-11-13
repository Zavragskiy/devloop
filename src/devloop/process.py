"""Run a child process and decode its output without a Unicode surprise.

``subprocess`` text mode decodes with the locale code page. A child that writes
UTF-8 while the code page is, say, cp1251 can emit a byte the code page does not
map, and Python's reader thread raises ``UnicodeDecodeError`` inside a thread:
the caller sees an empty stdout and, in a test step, an empty log instead of the
traceback. So devloop captures bytes and decodes UTF-8 first, the locale code
page second, and only then replaces the undecodable bytes.

Newlines are normalised the way text mode did, so callers keep seeing ``\\n``.

A command line also has a hard length limit, and it is the gate that breaks
first on a large tree: ``CreateProcess`` on Windows rejects anything over 32767
characters with ``ERROR_FILENAME_EXCED_RANGE``, which Python surfaces as
``FileNotFoundError: [WinError 206]``; Linux rejects a whole argv over
``ARG_MAX``. Neither message names the step that overflowed, so the length is
measured here and refused with a sentence a caller can act on. Callers that hand
over a whole tree batch the paths instead of reaching this guard.
"""

from __future__ import annotations

import locale
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_COMMAND_LINE = 32767 if os.name == "nt" else 1_000_000


def command_length(command: list[str]) -> int:
    """The number of characters the OS receives when this command is spawned."""
    return len(subprocess.list2cmdline(command))


@dataclass(frozen=True)
class Result:
    """The exit code and the decoded streams of one finished command."""

    returncode: int
    stdout: str
    stderr: str


def decode(data: bytes) -> str:
    """UTF-8 first, then the locale code page, then replacement characters."""
    if not data:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return _newlines(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return _newlines(data.decode("utf-8", errors="replace"))


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n")


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> Result:
    """Run ``command`` in ``cwd`` and decode what it printed."""
    length = command_length(command)
    if length > MAX_COMMAND_LINE:
        raise ValueError(
            f"the command line for {Path(command[0]).name} is {length} characters, over the "
            f"{MAX_COMMAND_LINE}-character limit of this platform; pass fewer paths "
            "(devloop batches the gated lists, so a project command or the test runner is "
            "the one to shorten)"
        )
    completed = subprocess.run(command, cwd=cwd, capture_output=True, env=env, check=False)
    return Result(
        returncode=completed.returncode,
        stdout=decode(completed.stdout),
        stderr=decode(completed.stderr),
    )
