"""Centralized logging configuration for TestMirror."""

from __future__ import annotations

import logging
import sys
from typing import Optional

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, stream=None) -> None:
    """Configure the root logger once (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    setup_logging()
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger
