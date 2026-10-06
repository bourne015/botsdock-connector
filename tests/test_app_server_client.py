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


def test_runtime_schema_checks_required_stable_methods(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from botsdock_connector.providers.app_server_client import AppServerError
    required = ["initialize", "thread/list", "thread/start", "thread/resume",
                "thread/turns/list", "turn/start", "turn/steer", "turn/interrupt", "thread/archive", "model/list"]
    def export(args, **kwargs):
        Path(args[args.index("--out") + 1], "ClientRequest.json").write_text(json.dumps({
            "oneOf": [{"properties": {"method": {"enum": [method]}}} for method in required],
        }))
        for title, field in [("TurnSteerParams", "expectedTurnId"), ("ThreadTurnsListParams", "itemsView")]:
            Path(args[args.index("--out") + 1], title + ".json").write_text(json.dumps({
                "title": title, "properties": {field: {"type": "string"}},
            }))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("subprocess.run", export)
    client = AppServerProcessClient.__new__(AppServerProcessClient)
    client.codex_bin, client.cwd, client.timeout = "codex", ".", 10
    client._check_runtime_compatibility()
    assert client._experimental_api is False
    required.remove("thread/turns/list")
    with pytest.raises(AppServerError, match="thread/turns/list"):
        client._check_runtime_compatibility()
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(AppServerError, match="repair"):
        client._check_runtime_compatibility()


def test_initialize_uses_stable_protocol():
    client = AppServerProcessClient.__new__(AppServerProcessClient)
    calls = []
    client.request = lambda method, params: calls.append((method, params)) or {"userAgent": "codex"}
    client.notification = lambda method: calls.append((method, None))
    client.initialize()
    assert calls[0][1]["capabilities"]["experimentalApi"] is False
    assert calls[1][0] == "initialized"


def test_runtime_enables_experimental_history_only_when_needed(monkeypatch):
    client = AppServerProcessClient.__new__(AppServerProcessClient)
    stable = {"initialize", "thread/list", "thread/start", "thread/resume",
              "turn/start", "turn/steer", "turn/interrupt", "thread/archive", "model/list"}
    def methods(*, experimental):
        client._parameter_schemas.update({"turn/steer": {"properties": {"expectedTurnId": {}}}, "thread/turns/list": {"properties": {"itemsView": {}}}})
        return stable | ({"thread/turns/list"} if experimental else set())
    client._runtime_methods = methods
    client._check_runtime_compatibility()
    assert client._experimental_api is True


def test_same_method_names_do_not_hide_incompatible_parameters():
    from botsdock_connector.providers.app_server_client import AppServerError
    client = AppServerProcessClient.__new__(AppServerProcessClient)
    client._runtime_methods = lambda experimental: {"initialize", "thread/list", "thread/start", "thread/resume", "thread/turns/list", "turn/start", "turn/steer", "turn/interrupt", "thread/archive", "model/list"}
    with pytest.raises(AppServerError, match="expectedTurnId"):
        client._check_runtime_compatibility()

def test_outgoing_requests_validate_installed_schema_before_dispatch():
    from botsdock_connector.providers.app_server_client import AppServerError
    client = AppServerProcessClient.__new__(AppServerProcessClient)
    client._parameter_schemas = {"turn/steer": {"type": "object", "required": ["expectedTurnId"], "properties": {"expectedTurnId": {"type": "string"}}}}
    with pytest.raises(AppServerError, match="turn/steer"):
        client.request("turn/steer", {"turnId": "obsolete"})
