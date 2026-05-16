"""Coalesce tiny streaming deltas before sending them to the backend."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from ..log import get_logger

logger = get_logger(__name__)

JsonDict = dict[str, Any]

DEFAULT_DELTA_FLUSH_INTERVAL_SECONDS = 0.12
DEFAULT_DELTA_FLUSH_CHARS = 768


COALESCE_EVENT_TYPES = {
    "assistant.delta",
    "command.output",
    "plan.delta",
    "reasoning.delta",
    "file.changed",
}


class BufferedBackendSender:
    """Coalesce tiny streaming deltas before sending them to the backend."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        outbound: asyncio.Queue[JsonDict],
        flush_interval: float = DEFAULT_DELTA_FLUSH_INTERVAL_SECONDS,
        max_chars: int = DEFAULT_DELTA_FLUSH_CHARS,
    ) -> None:
        self.loop = loop
        self.outbound = outbound
        self.flush_interval = max(0.02, flush_interval)
        self.max_chars = max(1, max_chars)
        self._lock = threading.Lock()
        self._buffers: dict[tuple[Any, ...], JsonDict] = {}
        self._flush_scheduled = False
        self._closed = False

    def send(self, message: JsonDict) -> None:
        if self._closed:
            return
        key = self._coalesce_key(message)
        if key is None:
            self.flush_all()
            self._put(message)
            return
        if message.get("event_type") == "file.changed":
            self._buffer_file_change(key, message)
            return
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            self.flush_all()
            self._put(message)
            return
        ready: JsonDict | None = None
        with self._lock:
            entry = self._buffers.get(key)
            if entry is None:
                buffered = dict(message)
                buffered_payload = dict(payload)
                buffered_payload["text"] = ""
                buffered["payload"] = buffered_payload
                entry = {"message": buffered, "parts": [], "chars": 0}
                self._buffers[key] = entry
                self._schedule_flush_locked()
            entry["parts"].append(text)
            entry["chars"] += len(text)
            if entry["chars"] >= self.max_chars:
                ready = self._buffers.pop(key)
        if ready is not None:
            self._put(self._materialize(ready))

    def _buffer_file_change(self, key: tuple[Any, ...], message: JsonDict) -> None:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        paths = _file_change_paths(payload)
        if not paths and not isinstance(payload.get("changes"), list):
            self.flush_all()
            self._put(message)
            return
        with self._lock:
            entry = self._buffers.get(key)
            if entry is None:
                buffered = dict(message)
                buffered["payload"] = _merged_file_change_payload({}, payload)
                self._buffers[key] = {"message": buffered}
                self._schedule_flush_locked()
                return
            current = entry["message"]
            current["payload"] = _merged_file_change_payload(current.get("payload") or {}, payload)

    def flush_all(self) -> None:
        with self._lock:
            entries = list(self._buffers.values())
            self._buffers.clear()
            self._flush_scheduled = False
        for entry in entries:
            self._put(self._materialize(entry))

    def close(self) -> None:
        self.flush_all()
        self._closed = True

    def _schedule_flush_locked(self) -> None:
        if self._flush_scheduled:
            return
        self._flush_scheduled = True

        def schedule() -> None:
            if not self._closed:
                self.loop.call_later(self.flush_interval, self.flush_all)

        self.loop.call_soon_threadsafe(schedule)

    def _put(self, message: JsonDict) -> None:
        if not self._closed:
            asyncio.run_coroutine_threadsafe(self.outbound.put(message), self.loop)

    @staticmethod
    def _materialize(entry: JsonDict) -> JsonDict:
        message = dict(entry["message"])
        if "parts" not in entry:
            message["payload"] = dict(message.get("payload") or {})
            return message
        payload = dict(message.get("payload") or {})
        payload["text"] = "".join(entry["parts"])
        message["payload"] = payload
        return message

    @staticmethod
    def _coalesce_key(message: JsonDict) -> tuple[Any, ...] | None:
        if message.get("type") != "app_server.event":
            return None
        event_type = message.get("event_type")
        if event_type not in COALESCE_EVENT_TYPES:
            return None
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if event_type == "file.changed":
            return (event_type, message.get("thread_id"), payload.get("watch_id"))
        return (
            event_type,
            message.get("thread_id"),
            message.get("turn_id"),
            payload.get("item_id") if event_type in {"command.output", "reasoning.delta"} else None,
        )


def _file_change_paths(payload: JsonDict) -> list[str]:
    values: list[str] = []
    for raw in (payload.get("paths"), payload.get("changed_paths"), payload.get("changedPaths")):
        if isinstance(raw, list):
            values.extend(_string_value(item) for item in raw if _string_value(item))
        elif _string_value(raw):
            values.append(_string_value(raw))
    if _string_value(payload.get("path")):
        values.append(_string_value(payload.get("path")))
    return _unique_strings(values)


def _merged_file_change_payload(current: JsonDict, incoming: JsonDict) -> JsonDict:
    merged = dict(current)
    for key, value in incoming.items():
        if key not in {"paths", "changed_paths", "changedPaths", "path", "changes"} and value is not None:
            merged[key] = value
    paths = _unique_strings([*_file_change_paths(current), *_file_change_paths(incoming)])
    if paths:
        merged["paths"] = paths
        merged["changed_paths"] = paths
        merged["path"] = paths[0]
    changes = _unique_changes(current.get("changes"), incoming.get("changes"))
    if changes:
        merged["changes"] = changes
    return merged


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _unique_changes(*groups: Any) -> list[JsonDict]:
    result: list[JsonDict] = []
    seen: set[tuple[Any, ...]] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("path"),
                item.get("change_type") or item.get("changeType"),
                item.get("diff"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(item))
    return result


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None
