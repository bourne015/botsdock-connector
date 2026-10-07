"""Structured logging for botsdock-connector."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_log_initialized = False


class _RotatingStream:
    encoding = 'utf-8'

    def __init__(self, handler):
        self.handler = handler

    def write(self, text):
        if not text:
            return 0
        handler = self.handler
        handler.acquire()
        try:
            chunk_size = max(1, handler.maxBytes // 4)
            for offset in range(0, len(text), chunk_size):
                chunk = text[offset:offset + chunk_size]
                if handler.stream.tell() + len(chunk.encode('utf-8')) >= handler.maxBytes:
                    handler.doRollover()
                handler.stream.write(chunk)
            handler.flush()
        finally:
            handler.release()
        return len(text)

    def flush(self):
        self.handler.flush()

    def isatty(self):
        return False


def init_logging() -> None:
    global _log_initialized
    if _log_initialized:
        return
    level_name = os.environ.get("BOTSDOCK_CONNECTOR_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)

    if os.environ.get('BOTSDOCK_CONNECTOR_DAEMON') == '1':
        path = Path(os.environ.get('BOTSDOCK_CONNECTOR_LOG_FILE') or
                    Path.home() / '.botsdock' / 'botsdock_connector.log').expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                       backupCount=2, encoding='utf-8')
        sys.stdout = sys.stderr = _RotatingStream(rotating)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))

    root = logging.getLogger("botsdock_connector")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False

    _log_initialized = True


def get_logger(name: str) -> logging.Logger:
    init_logging()
    # Callers pass __name__, which already includes the package prefix.
    return logging.getLogger(name)
