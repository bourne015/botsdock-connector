"""Structured logging for botsdock-connector."""

from __future__ import annotations

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_log_initialized = False


def init_logging() -> None:
    global _log_initialized
    if _log_initialized:
        return
    level_name = os.environ.get("BOTSDOCK_CONNECTOR_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))

    root = logging.getLogger("botsdock_connector")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False

    _log_initialized = True


def get_logger(name: str) -> logging.Logger:
    init_logging()
    return logging.getLogger(f"botsdock_connector.{name}")
