"""Diagnostic logging to stderr only.

stdout carries the MCP protocol stream, so nothing in this module may ever
write to it. The handler binds to ``sys.stderr`` lazily at emit time so tests
(and callers that swap stderr) see the output where they expect it.
"""

from __future__ import annotations

import logging
import sys
from typing import override

LEVELS = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

logger = logging.getLogger("envelope")


class _StderrHandler(logging.Handler):
    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


_handler = _StderrHandler()
_handler.setFormatter(logging.Formatter("envelope %(levelname)s: %(message)s"))
logger.addHandler(_handler)
logger.propagate = False
logger.setLevel(logging.WARNING)


def set_level(level: str) -> None:
    logger.setLevel(LEVELS[level])
