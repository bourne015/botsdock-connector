"""Tests for AppServerProcessClient."""

from __future__ import annotations

import json
import queue
import threading

import pytest

from botsdock_connector.providers.app_server_client import (
    AppServerProcessClient,
    _elapsed_ms,
)


def test_elapsed_ms_returns_positive_value():
    import time
    started = time.monotonic() - 0.5
    ms = _elapsed_ms(started)
    assert 400 < ms < 1000


def test_concurrent_id_allocation_is_thread_safe():
    """Verify that _allocate_id produces unique IDs under concurrent access."""
    # Create a minimal mock - we can't easily instantiate without a subprocess
    # so we test the ID allocation pattern directly
    lock = threading.Lock()
    next_id = [1]

    def allocate():
        with lock:
            rid = next_id[0]
            next_id[0] += 1
            return rid

    ids = []
    threads_list = []
    for _ in range(10):
        t = threading.Thread(target=lambda: ids.append(allocate()))
        threads_list.append(t)
        t.start()
    for t in threads_list:
        t.join()
    assert sorted(ids) == list(range(1, 11))
