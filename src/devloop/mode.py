"""The working mode of the edit loop: ``strict`` (the default) or ``draft``.

In draft a plain ``check`` still runs every step, but only what means "the code
is broken" fails the run: a file that does not parse, a tool that fails without
a finding, and whatever ``draft_blocking`` lists. Every other new finding is
counted as deferred and blocks later, at ``--full``. ``--full`` is always strict,
so nothing deferred can pass the commit gate.

The mode comes from the first source that names one: ``--mode``, then
``DEVLOOP_MODE``, then ``<cache_dir>/mode`` (written by ``devloop mode``), then
``[tool.devloop.modes] default``. When ``CI`` is set the mode is strict whatever
the source says.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devloop.config import MODES, Config
from devloop.findings import Finding

MODE_FILE = "mode"
ENV = "DEVLOOP_MODE"
SYNTAX_CODES = frozenset({"invalid-syntax", "E999", "misc"})
"""Ruff codes of a file that does not parse; ``misc`` is a finding without a code."""


@dataclass(frozen=True)
class Mode:
    """The resolved mode, where it came from, and a source it overrode."""

    name: str
    source: str
    ignored: str = ""

    @property
    def draft(self) -> bool:
        return self.name == "draft"

    @property
    def header(self) -> str:
        detail = f"{self.source}; {self.ignored}" if self.ignored else self.source
        return f"mode: {self.name} ({detail})"

    @property
    def quiet_default(self) -> bool:
        """True for the plain strict default, which prints no mode line."""
        return self.name == "strict" and self.source == "default" and not self.ignored


@dataclass(frozen=True)
class Blocking:
    """What a draft run still fails on."""

    steps: frozenset[str]
    ruff_codes: tuple[str, ...]

    @classmethod
    def parse(cls, entries: tuple[str, ...]) -> Blocking:
        steps: set[str] = set()
        codes: tuple[str, ...] = ()
        for entry in entries:
            step, _, selectors = entry.partition(":")
            if selectors:
                codes = tuple(selectors.split(","))
            else:
                steps.add(step)
        return cls(frozenset(steps), codes)

    def blocks(self, step: str, finding: Finding) -> bool:
        if step in self.steps:
            return True
        if step != "ruff":
            return False
        code = finding.key.rsplit(":", 1)[-1]
        return code in SYNTAX_CODES or code.startswith(self.ruff_codes)


def mode_file(cfg: Config) -> Path:
    return cfg.cache_dir / MODE_FILE


def _checked(value: str, origin: str) -> str:
    clean = value.strip()
    if clean not in MODES:
        raise ValueError(f"{origin} must be one of {', '.join(MODES)}, not {clean!r}")
    return clean


def read_file(cfg: Config) -> str | None:
    """The mode ``devloop mode`` wrote for this checkout, or ``None``."""
    path = mode_file(cfg)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return _checked(text, str(path))


def write_file(cfg: Config, name: str | None) -> None:
    """Pin this checkout to ``name``, or remove the pin when ``name`` is ``None``."""
    path = mode_file(cfg)
    if name is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_checked(name, "the mode") + "\n", encoding="utf-8")


def _in_ci(environ: Mapping[str, str]) -> bool:
    return environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def chain(
    cfg: Config, flag: str | None, environ: Mapping[str, str] | None = None
) -> list[tuple[str, str | None]]:
    """Every source in resolution order with the mode it names, or ``None``."""
    env = os.environ if environ is None else environ
    raw_env = env.get(ENV, "").strip()
    return [
        ("--mode", flag),
        (ENV, _checked(raw_env, ENV) if raw_env else None),
        ("file", read_file(cfg)),
        ("default", cfg.mode_default),
    ]


def resolve(
    cfg: Config,
    flag: str | None,
    *,
    full: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Mode:
    """The mode of this run; ``--full`` and ``CI`` always resolve to strict."""
    env = os.environ if environ is None else environ
    source, name = next((src, value) for src, value in chain(cfg, flag, env) if value)
    if name != "draft":
        return Mode(name, source)
    if full:
        if source == "--mode":
            raise ValueError("--full is always strict; --mode draft contradicts it")
        return Mode("strict", "--full", f"draft from {source} ignored")
    if _in_ci(env):
        return Mode("strict", "CI", f"draft from {source} ignored")
    return Mode("draft", source)
