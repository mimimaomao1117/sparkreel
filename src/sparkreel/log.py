"""Tiny logging helper — one configured `sparkreel` logger, opt-in verbosity.

The pipeline does a lot of best-effort work (optional face models, network B-roll,
ffmpeg fallbacks) that historically failed *silently*. Those key paths now log
through here instead of swallowing the error, so a failed YuNet load or a B-roll
endpoint timeout is visible without changing behaviour.

Quiet by default (WARNING — only genuine problems surface). Turn it up to trace
what the analysis is doing:

    SPARKREEL_LOG=debug    # or: info | warning | error | quiet
"""
from __future__ import annotations

import logging
import os

_LEVELS = {
    "debug": logging.DEBUG, "info": logging.INFO,
    "warning": logging.WARNING, "warn": logging.WARNING,
    "error": logging.ERROR, "quiet": logging.CRITICAL,
}


def get_logger(name: str = "sparkreel") -> logging.Logger:
    """Return a logger under the configured `sparkreel` root (child if `name` is a
    submodule, e.g. ``get_logger(__name__)``)."""
    root = logging.getLogger("sparkreel")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(_LEVELS.get(
            os.environ.get("SPARKREEL_LOG", "warning").strip().lower(), logging.WARNING))
        root.propagate = False   # own handler only — don't double-log via the py root
    return logging.getLogger(name if name.startswith("sparkreel") else f"sparkreel.{name}")
