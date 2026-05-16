"""Tests for BufferedBackendSender delta coalescing."""

from __future__ import annotations

import asyncio

import pytest

from botsdock_connector.providers.delta_buffer import (
    BufferedBackendSender,
    COALESCE_EVENT_TYPES,
    _file_change_paths,
    _merged_file_change_payload,
    _unique_changes,
    _unique_strings,
)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def make_delta(thread_id="t1", turn_id="tn1", text="hello", event_type="assistant.delta"):
    return {
        "type": "app_server.event",
        "event_type": event_type,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "payload": {"text": text},
    }


def test_coalesce_key_non_app_server_event_returns_none():
    assert BufferedBackendSender._coalesce_key({"type": "other"}) is None


def test_coalesce_key_non_streaming_type_returns_none():
    for event_type in ("turn.started", "assistant.message", "approval.requested"):
        assert BufferedBackendSender._coalesce_key(
            {"type": "app_server.event", "event_type": event_type}
        ) is None


def test_coalesce_key_streaming_events():
    for event_type in COALESCE_EVENT_TYPES:
        if event_type == "file.changed":
            key = BufferedBackendSender._coalesce_key(
                {
                    "type": "app_server.event",
                    "event_type": event_type,
                    "thread_id": "t1",
                    "payload": {"watch_id": "w1"},
                }
            )
            assert key == (event_type, "t1", "w1")
        elif event_type in {"command.output", "reasoning.delta"}:
            key = BufferedBackendSender._coalesce_key(
                {
                    "type": "app_server.event",
                    "event_type": event_type,
                    "thread_id": "t1",
                    "turn_id": "tn1",
                    "payload": {"item_id": "i1"},
                }
            )
            assert key == (event_type, "t1", "tn1", "i1")
        else:
            key = BufferedBackendSender._coalesce_key(
                {
                    "type": "app_server.event",
                    "event_type": event_type,
                    "thread_id": "t1",
                    "turn_id": "tn1",
                }
            )
            assert key == (event_type, "t1", "tn1", None)


def test_materialize_joins_delta_parts():
    entry = {
        "message": {
            "type": "app_server.event",
            "event_type": "assistant.delta",
            "payload": {"text": ""},
        },
        "parts": ["Hel", "lo ", "World"],
        "chars": 11,
    }
    result = BufferedBackendSender._materialize(entry)
    assert result["payload"]["text"] == "Hello World"


def test_materialize_no_parts():
    entry = {
        "message": {
            "type": "app_server.event",
            "event_type": "file.changed",
            "payload": {"paths": ["/tmp/a.txt"]},
        },
    }
    result = BufferedBackendSender._materialize(entry)
    assert result["payload"]["paths"] == ["/tmp/a.txt"]


def test_file_change_paths_consolidates_sources():
    payload = {
        "paths": ["/a", "/b"],
        "path": "/c",
        "changed_paths": [],
        "changedPaths": ["/d"],
    }
    paths = _file_change_paths(payload)
    assert sorted(paths) == ["/a", "/b", "/c", "/d"]


def test_unique_strings_dedup():
    assert _unique_strings(["a", "b", "a", "c"]) == ["a", "b", "c"]


def test_unique_changes_dedup_by_key():
    a = {"path": "/a", "change_type": "edit", "diff": "..."}
    b = {"path": "/a", "change_type": "edit", "diff": "..."}
    c = {"path": "/b", "change_type": "add"}
    result = _unique_changes([a, b], [c])
    assert len(result) == 2


def test_merged_file_change_payload_combines_paths():
    current = {"paths": ["/a"]}
    incoming = {"paths": ["/b"]}
    merged = _merged_file_change_payload(current, incoming)
    assert sorted(merged["paths"]) == ["/a", "/b"]


def test_coalesce_key_includes_item_id_for_command_output():
    msg = {
        "type": "app_server.event",
        "event_type": "command.output",
        "thread_id": "t1",
        "turn_id": "tn1",
        "payload": {"item_id": "item_1", "text": "out"},
    }
    key = BufferedBackendSender._coalesce_key(msg)
    assert key == ("command.output", "t1", "tn1", "item_1")
