"""Codex app-server connector for Bots Dock Codex Remote Console.

The connector runs on a user's machine. It connects outbound to the backend
WebSocket, starts a local `codex app-server` over stdio, forwards backend
requests to app-server JSON-RPC, and reports normalized events back.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..log import get_logger
from .app_server_client import AppServerError, ConnectorError
from .delta_buffer import BufferedBackendSender

logger = get_logger(__name__)

JsonDict = dict[str, Any]

TIMING_EVENT_TYPES = {
    "thread.started",
    "thread.resumed",
    "thread.status",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "assistant.message",
    "approval.requested",
    "approval.resolved",
    "user_input.requested",
    "user_input.resolved",
}


def _version_label(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"\b\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\b", text)
    if match:
        return match.group(0)
    return text[:64]


def _is_thread_not_found_error(error: Exception) -> bool:
    return "thread not found" in str(error).lower()


def _elapsed_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1000.0


def _should_log_timing_event(event_type: Any) -> bool:
    return isinstance(event_type, str) and event_type in TIMING_EVENT_TYPES


class CodexConnector:
    def __init__(self, *, app_server: Any, cwd: str, model: str | None = None) -> None:
        self.app_server = app_server
        self.cwd = cwd
        self.model = model
        self.backend_send: Callable[[JsonDict], Any] | None = None
        self.thread_map: dict[str, str] = {}
        self.reverse_thread_map: dict[str, str] = {}
        self.turn_map: dict[str, str] = {}
        self.reverse_turn_map: dict[str, str] = {}
        self.pending_approval_requests: dict[str, JsonDict] = {}
        self.pending_approval_events: dict[str, JsonDict] = {}
        self.pending_unmapped_server_requests: dict[str, JsonDict] = {}
        self.assistant_delta_buffers: dict[tuple[str, str | None, str | None], list[str]] = {}
        self.fs_watch_map: dict[str, JsonDict] = {}

    def bind_backend_sender(self, sender: Callable[[JsonDict], Any]) -> None:
        self.backend_send = sender

    def initialize_app_server(self) -> JsonDict:
        if hasattr(self.app_server, "initialize"):
            return self.app_server.initialize()
        return {}

    def hello(self, *, connector_version: str = __version__) -> JsonDict:
        return {
            "type": "connector.hello",
            "provider": "codex",
            "connector_version": connector_version,
            "platform": sys.platform,
            "hostname": socket.gethostname(),
            "protocol_version": "0.1",
            "capabilities": [
                "app_server.thread_list",
                "app_server.thread_start",
                "app_server.thread_resume",
                "app_server.turn_start",
                "app_server.turn_cancel",
                "app_server.approval_respond",
                "app_server.fs_watch",
                "app_server.fs_unwatch",
                "app_server.history_cursor",
                "app_server.account_snapshot",
                "app_server.thread_unsubscribe",
                "app_server.thread_archive",
                "app_server.thread_unarchive",
                "app_server.user_input_placeholder",
                "workspace.report",
            ],
            "app_server": {
                "version": None,
                "transport": "stdio",
            },
        }

    def workspace_report(self) -> JsonDict:
        path = str(Path(self.cwd).resolve())
        branch = _git_branch(path)
        return {
            "type": "workspace.report",
            "workspaces": [
                {
                    "name": Path(path).name or path,
                    "path": path,
                    "current_branch": branch,
                }
            ] if path else [],
        }

    def thread_sync_report(self, *, limit: int = 200) -> JsonDict:
        result = self.app_server.request("thread/list", {"limit": limit, "archived": False})
        threads = _extract_thread_list(result)
        workspaces: dict[str, JsonDict] = {}
        normalized_threads = []
        for thread in threads:
            normalized = _normalize_thread_for_sync(thread)
            if not normalized.get("app_server_thread_id"):
                continue
            normalized_threads.append(normalized)
            remote_path = normalized.get("remote_path")
            if remote_path and remote_path not in workspaces:
                workspaces[remote_path] = {
                    "name": Path(remote_path).name or remote_path,
                    "path": remote_path,
                    "current_branch": normalized.get("current_branch") or _git_branch(remote_path),
                }
        return {
            "type": "thread.sync",
            "threads": normalized_threads,
            "workspaces": list(workspaces.values()),
            "raw_payload": result,
        }

    def handle_backend_message(self, message: JsonDict) -> JsonDict | None:
        msg_type = message.get("type")
        request_id = message.get("request_id")
        try:
            if msg_type == "app_server.turn_start":
                payload = self._handle_turn_start(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.turn_steer":
                payload = self._handle_turn_steer(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.turn_cancel":
                payload = self._handle_turn_cancel(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.approval_respond":
                payload = self._handle_approval_respond(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.thread_resume":
                payload = self._handle_thread_resume(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.thread_archive":
                payload = self._handle_thread_archive(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.thread_unarchive":
                payload = self._handle_thread_unarchive(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.thread_delete":
                raise ConnectorError("thread_delete_not_supported")
            if msg_type == "app_server.fs_watch":
                payload = self._handle_fs_watch(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.fs_unwatch":
                payload = self._handle_fs_unwatch(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "connector.thread_history":
                payload = self._handle_thread_history(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.thread_list":
                payload = self._handle_thread_list(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "app_server.account_snapshot":
                payload = self._handle_account_snapshot(message.get("payload") or {})
                return self._ok(request_id, payload)
            if msg_type == "connector.sync_snapshot":
                payload = self.thread_sync_report(limit=(message.get("payload") or {}).get("limit") or 200)
                return self._ok(request_id, payload)
            if msg_type == "thread.sync_ack":
                self._handle_thread_sync_ack(message)
                return None
            if msg_type in {
                "workspace.report_ack",
                "connector.event_ack",
                "connector.transient_ack",
                "connector.heartbeat_ack",
                "app_server.request_opened_ack",
            }:
                return None
            if request_id is not None:
                return self._error(request_id, "unsupported_request", f"unsupported backend request type: {msg_type}")
            return None
        except Exception as exc:
            logger.error(
                "codex connector request failed: type=%s request=%s error=%s",
                msg_type, request_id, exc,
            )
            return self._error(request_id, "connector_error", str(exc))

    def handle_appserver_message(self, message: JsonDict) -> None:
        started_at = time.monotonic()
        if "method" not in message:
            return
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" in message:
            event = self._normalize_server_request(message, method, params)
        else:
            event = self._normalize_notification(message, method, params)
        if event is not None:
            self._send_backend(event)
            event_type = event.get("event_type")
            if _should_log_timing_event(event_type):
                logger.info(
                    "codex connector timing appserver_event: "
                    "method=%s event_type=%s thread=%s turn=%s handle_ms=%.1f",
                    method, event_type, event.get("thread_id"), event.get("turn_id"),
                    _elapsed_ms(started_at),
                )

    def _handle_turn_start(self, payload: JsonDict) -> JsonDict:
        handler_started_at = time.monotonic()
        internal_thread_id = payload.get("thread_id")
        internal_turn_id = payload.get("turn_id")
        prompt = payload.get("prompt") or ""
        if not internal_thread_id or not internal_turn_id or not prompt:
            raise ConnectorError("turn_start requires thread_id, turn_id, and prompt")
        app_thread_id = payload.get("app_server_thread_id") or self.thread_map.get(internal_thread_id)
        if not app_thread_id:
            app_thread_id = self._start_thread(internal_thread_id, payload)
        else:
            self.thread_map[internal_thread_id] = app_thread_id
            self.reverse_thread_map[app_thread_id] = internal_thread_id
        try:
            request_started_at = time.monotonic()
            result = self.app_server.request("turn/start", self._turn_start_params(app_thread_id, prompt, payload))
            request_ms = _elapsed_ms(request_started_at)
        except AppServerError as exc:
            if not _is_thread_not_found_error(exc):
                raise
            logger.info(
                "codex connector app-server thread not loaded: "
                "thread=%s app_thread=%s; resuming",
                internal_thread_id, app_thread_id,
            )
            self._resume_app_thread(internal_thread_id, app_thread_id, payload)
            request_started_at = time.monotonic()
            result = self.app_server.request("turn/start", self._turn_start_params(app_thread_id, prompt, payload))
            request_ms = _elapsed_ms(request_started_at)
        turn = result.get("turn") or {}
        app_turn_id = turn.get("id")
        if app_turn_id:
            self.turn_map[internal_turn_id] = app_turn_id
            self.reverse_turn_map[app_turn_id] = internal_turn_id
        self._send_backend_event(
            "turn.started",
            internal_thread_id,
            internal_turn_id,
            {
                "app_server_thread_id": app_thread_id,
                "app_server_turn_id": app_turn_id,
                "status": turn.get("status") or "inProgress",
            },
            result,
        )
        logger.info(
            "codex connector timing turn_start: "
            "thread=%s turn=%s app_thread=%s app_turn=%s request_ms=%.1f total_ms=%.1f",
            internal_thread_id, internal_turn_id,
            app_thread_id, app_turn_id,
            request_ms, _elapsed_ms(handler_started_at),
        )
        return {"app_server_thread_id": app_thread_id, "app_server_turn_id": app_turn_id}

    def _turn_start_params(self, app_thread_id: str, prompt: str, payload: JsonDict) -> JsonDict:
        turn_params: JsonDict = {
            "threadId": app_thread_id,
            "input": [
                {
                    "type": "text",
                    "text": prompt,
                    "text_elements": [],
                }
            ],
            "approvalPolicy": payload.get("approval_policy") or "on-request",
            "approvalsReviewer": "user",
            "sandboxPolicy": _sandbox_policy(payload.get("cwd") or self.cwd),
        }
        model = payload.get("model") or self.model
        if model:
            turn_params["model"] = model
        effort = payload.get("reasoning_effort") or payload.get("effort")
        if effort:
            turn_params["effort"] = effort
        return turn_params

    def _handle_turn_steer(self, payload: JsonDict) -> JsonDict:
        internal_thread_id = payload.get("thread_id")
        internal_turn_id = payload.get("turn_id")
        prompt = payload.get("prompt") or ""
        if not internal_thread_id or not internal_turn_id or not prompt:
            raise ConnectorError("turn_steer requires thread_id, turn_id, and prompt")
        app_thread_id = payload.get("app_server_thread_id") or self.thread_map.get(internal_thread_id)
        app_turn_id = payload.get("app_server_turn_id") or self.turn_map.get(internal_turn_id)
        if not app_thread_id or not app_turn_id:
            raise ConnectorError("turn_steer requires loaded app-server thread and turn ids")
        self.thread_map[internal_thread_id] = app_thread_id
        self.reverse_thread_map[app_thread_id] = internal_thread_id
        self.turn_map[internal_turn_id] = app_turn_id
        self.reverse_turn_map[app_turn_id] = internal_turn_id
        result = self.app_server.request(
            "turn/steer",
            {
                "threadId": app_thread_id,
                "turnId": app_turn_id,
                "input": [
                    {
                        "type": "text",
                        "text": prompt,
                        "text_elements": [],
                    }
                ],
            },
        )
        return {
            "app_server_thread_id": app_thread_id,
            "app_server_turn_id": app_turn_id,
            "raw": result,
        }

    def _start_thread(self, internal_thread_id: str, payload: JsonDict) -> str:
        started_at = time.monotonic()
        cwd = payload.get("cwd") or self.cwd
        params: JsonDict = {
            "cwd": cwd,
            "approvalPolicy": payload.get("approval_policy") or "on-request",
            "approvalsReviewer": "user",
            "sandbox": payload.get("sandbox") or "workspace-write",
            "ephemeral": bool(payload.get("ephemeral", False)),
            "experimentalRawEvents": False,
            "persistExtendedHistory": False,
        }
        model = payload.get("model") or self.model
        if model:
            params["model"] = model
        result = self.app_server.request("thread/start", params)
        request_ms = _elapsed_ms(started_at)
        thread = result.get("thread") or {}
        app_thread_id = thread.get("id")
        if not app_thread_id:
            raise ConnectorError("thread/start did not return thread.id")
        self.thread_map[internal_thread_id] = app_thread_id
        self.reverse_thread_map[app_thread_id] = internal_thread_id
        self._send_backend_event(
            "thread.started",
            internal_thread_id,
            None,
            {
                "app_server_thread_id": app_thread_id,
                "title": thread.get("name") or thread.get("title"),
                "cwd": thread.get("cwd") or cwd,
            },
            result,
        )
        logger.info(
            "codex connector timing thread_start: "
            "thread=%s app_thread=%s request_ms=%.1f",
            internal_thread_id, app_thread_id, request_ms,
        )
        return app_thread_id

    def _handle_turn_cancel(self, payload: JsonDict) -> JsonDict:
        internal_thread_id = payload.get("thread_id")
        internal_turn_id = payload.get("turn_id")
        app_thread_id = payload.get("app_server_thread_id") or self.thread_map.get(internal_thread_id)
        app_turn_id = payload.get("app_server_turn_id") or self.turn_map.get(internal_turn_id)
        if not app_thread_id or not app_turn_id:
            raise ConnectorError("turn_cancel requires mapped app-server thread and turn ids")
        result = self.app_server.request("turn/interrupt", {"threadId": app_thread_id, "turnId": app_turn_id})
        return {"app_server_thread_id": app_thread_id, "app_server_turn_id": app_turn_id, "result": result}

    def _handle_approval_respond(self, payload: JsonDict) -> JsonDict:
        app_request_id = payload.get("app_server_request_id")
        if app_request_id is None:
            raise ConnectorError("approval_respond requires app_server_request_id")
        decision = payload.get("decision") or "decline"
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {"decision": decision}
        pending = self.pending_approval_requests.pop(str(app_request_id), None)
        if not isinstance(pending, dict):
            raise ConnectorError("approval_request_not_found")
        response_request_id = (
            pending.get("id")
            if pending.get("id") is not None
            else app_request_id
        )
        self.app_server.send_response(response_request_id, response)
        self.pending_approval_events.pop(str(app_request_id), None)
        self.pending_unmapped_server_requests.pop(str(app_request_id), None)
        return {"app_server_request_id": app_request_id, "decision": decision, "response": response}

    def _handle_thread_resume(self, payload: JsonDict) -> JsonDict:
        internal_thread_id = payload.get("thread_id")
        app_thread_id = payload.get("app_server_thread_id") or self.thread_map.get(internal_thread_id)
        if not internal_thread_id or not app_thread_id:
            raise ConnectorError("thread_resume requires thread_id and app_server_thread_id")
        result = self._resume_app_thread(internal_thread_id, app_thread_id, payload)
        return {"app_server_thread_id": app_thread_id, "result": result}

    def _handle_thread_archive(self, payload: JsonDict) -> JsonDict:
        internal_thread_id = payload.get("thread_id")
        app_thread_id = payload.get("app_server_thread_id") or self.thread_map.get(internal_thread_id)
        if not internal_thread_id:
            raise ConnectorError("thread_archive requires thread_id")
        if app_thread_id:
            try:
                self.app_server.request("thread/archive", {"threadId": app_thread_id})
            except Exception as exc:
                logger.warning(
                    "thread/archive request failed for thread=%s: %s",
                    internal_thread_id, exc,
                )
        self.thread_map.pop(internal_thread_id, None)
        if app_thread_id:
            self.reverse_thread_map.pop(app_thread_id, None)
        turn_ids_to_remove = [
            turn_id
            for turn_id, tid in list(self.turn_map.items())
            if tid == internal_thread_id
        ]
        for turn_id in turn_ids_to_remove:
            rev = self.turn_map.pop(turn_id, None)
            if rev:
                self.reverse_turn_map.pop(rev, None)
        return {
            "thread_id": internal_thread_id,
            "app_server_thread_id": app_thread_id,
            "archived": True,
        }

    def _handle_thread_unarchive(self, payload: JsonDict) -> JsonDict:
        internal_thread_id = payload.get("thread_id")
        app_thread_id = payload.get("app_server_thread_id") or self.thread_map.get(internal_thread_id)
        if not internal_thread_id:
            raise ConnectorError("thread_unarchive requires thread_id")
        if not app_thread_id:
            raise ConnectorError("thread_unarchive requires app_server_thread_id")
        self.app_server.request("thread/unarchive", {"threadId": app_thread_id})
        self.thread_map[internal_thread_id] = app_thread_id
        self.reverse_thread_map[app_thread_id] = internal_thread_id
        return {"thread_id": internal_thread_id, "app_server_thread_id": app_thread_id, "unarchived": True}

    def _resume_app_thread(self, internal_thread_id: str, app_thread_id: str, payload: JsonDict) -> JsonDict:
        started_at = time.monotonic()
        self.thread_map[internal_thread_id] = app_thread_id
        self.reverse_thread_map[app_thread_id] = internal_thread_id
        result = self.app_server.request(
            "thread/resume",
            {
                "threadId": app_thread_id,
                "cwd": payload.get("cwd") or self.cwd,
                "persistExtendedHistory": False,
            },
        )
        self._flush_unmapped_server_requests()
        self._send_backend_event("thread.resumed", internal_thread_id, None, {"app_server_thread_id": app_thread_id}, result)
        logger.info(
            "codex connector timing thread_resume: "
            "thread=%s app_thread=%s request_ms=%.1f",
            internal_thread_id, app_thread_id, _elapsed_ms(started_at),
        )
        return result

    def _handle_thread_sync_ack(self, message: JsonDict) -> None:
        raw_threads = message.get("threads")
        if not isinstance(raw_threads, list):
            return
        for thread in raw_threads:
            if not isinstance(thread, dict):
                continue
            internal_thread_id = _string_value(thread.get("id"))
            app_thread_id = _string_value(
                thread.get("app_server_thread_id") or thread.get("appServerThreadId")
            )
            if not internal_thread_id or not app_thread_id:
                continue
            self.thread_map[internal_thread_id] = app_thread_id
            self.reverse_thread_map[app_thread_id] = internal_thread_id
        self._flush_unmapped_server_requests()

    def _handle_fs_watch(self, payload: JsonDict) -> JsonDict:
        internal_thread_id = payload.get("thread_id")
        path = payload.get("path") or payload.get("cwd") or self.cwd
        watch_id = payload.get("watch_id") or payload.get("watchId") or str(uuid.uuid4())
        if not internal_thread_id or not path:
            raise ConnectorError("fs_watch requires thread_id and path")
        result = self.app_server.request("fs/watch", {"watchId": watch_id, "path": path})
        self.fs_watch_map[str(watch_id)] = {
            "thread_id": internal_thread_id,
            "path": path,
        }
        return {"watch_id": watch_id, "path": path, "result": result}

    def _handle_fs_unwatch(self, payload: JsonDict) -> JsonDict:
        watch_id = payload.get("watch_id") or payload.get("watchId")
        if not watch_id:
            raise ConnectorError("fs_unwatch requires watch_id")
        result = self.app_server.request("fs/unwatch", {"watchId": watch_id})
        self.fs_watch_map.pop(str(watch_id), None)
        return {"watch_id": watch_id, "result": result}

    def _handle_thread_list(self, payload: JsonDict) -> JsonDict:
        result = self.app_server.request("thread/list", {"limit": payload.get("limit") or 50, "archived": False})
        return result

    def _handle_account_snapshot(self, payload: JsonDict) -> JsonDict:
        errors: JsonDict = {}
        account_result: JsonDict = {}
        rate_limits_result: JsonDict = {}
        refresh_token = _bool_value(_first_present(payload, "refresh_token", "refreshToken")) or False
        try:
            account_result = self.app_server.request(
                "account/read",
                {"refreshToken": refresh_token},
            )
        except Exception as exc:
            errors["account"] = str(exc)
        try:
            rate_limits_result = self.app_server.request("account/rateLimits/read")
        except Exception as exc:
            errors["rate_limits"] = str(exc)
        if not account_result and not rate_limits_result:
            raise ConnectorError(errors.get("account") or errors.get("rate_limits") or "failed to read Codex account")
        rate_limits, rate_limits_by_limit_id = _safe_rate_limits(rate_limits_result)
        return {
            "captured_at": int(time.time()),
            "account": _safe_account(account_result.get("account")),
            "requires_openai_auth": bool(account_result.get("requiresOpenaiAuth")),
            "rate_limits": rate_limits,
            "rate_limits_by_limit_id": rate_limits_by_limit_id,
            **({"errors": errors} if errors else {}),
        }

    def _handle_thread_history(self, payload: JsonDict) -> JsonDict:
        app_thread_id = payload.get("app_server_thread_id")
        if not app_thread_id:
            raise ConnectorError("thread_history requires app_server_thread_id")
        limit = int(payload.get("limit") or 20)
        limit = max(1, min(limit, 50))
        cursor = payload.get("cursor")
        direction = payload.get("direction") or ("older" if cursor else "latest")
        params: JsonDict = {"threadId": app_thread_id, "limit": limit, "sortDirection": "desc"}
        if cursor:
            params["cursor"] = cursor
        started_at = time.monotonic()
        logger.info(
            "codex connector thread history start: thread=%s direction=%s limit=%s",
            app_thread_id, direction, limit,
        )
        result = self.app_server.request("thread/turns/list", params)
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        turns = result.get("data") if isinstance(result.get("data"), list) else []
        if not turns:
            turns = result.get("turns") if isinstance(result.get("turns"), list) else []
        if not turns and isinstance(thread.get("turns"), list):
            turns = thread.get("turns")
        next_cursor = result.get("nextCursor") or result.get("next_cursor")
        backwards_cursor = result.get("backwardsCursor") or result.get("backwards_cursor")
        has_more_before = bool(next_cursor)
        item_count = sum(
            len(turn.get("items") if isinstance(turn.get("items"), list) else [])
            for turn in turns
            if isinstance(turn, dict)
        )
        backend_turns = list(reversed(turns)) if turns else []
        logger.info(
            "codex connector thread history done: "
            "thread=%s direction=%s turns=%s items=%s elapsed=%.2fs",
            app_thread_id, direction, len(turns), item_count,
            time.monotonic() - started_at,
        )
        return {
            "type": "thread.history",
            "direction": direction,
            "limit": limit,
            "has_more_before": has_more_before,
            "next_cursor": next_cursor,
            "backwards_cursor": backwards_cursor,
            "thread": {
                "id": thread.get("id"),
                "title": thread.get("title"),
                "status": thread.get("status"),
            },
            "turns": backend_turns,
        }

    def _normalize_server_request(self, message: JsonDict, method: str, params: JsonDict) -> JsonDict | None:
        app_thread_id = _app_thread_id_from_params(params)
        internal_thread_id = self.reverse_thread_map.get(app_thread_id)
        app_turn_id = _app_turn_id_from_params(params)
        internal_turn_id = self.reverse_turn_map.get(app_turn_id)
        request_id = message.get("id")
        if _is_auth_refresh_request(method):
            if request_id is not None:
                self.app_server.send_response(
                    request_id,
                    error={
                        "code": "unsupported_request",
                        "message": "ChatGPT auth token refresh is not available in Remote Console",
                    },
                )
            return None
        if _is_user_input_request(method) or _is_mcp_elicitation_request(method):
            if request_id is not None:
                self.app_server.send_response(
                    request_id,
                    error={
                        "code": "unsupported_request",
                        "message": f"unsupported app-server request: {method}",
                    },
                )
            if not internal_thread_id:
                return None
            return {
                "type": "app_server.event",
                "event_type": "user_input.requested",
                "thread_id": internal_thread_id,
                "turn_id": internal_turn_id,
                "payload": {
                    "request_id": request_id,
                    "app_server_request_id": request_id,
                    "user_input_id": params.get("inputRequestId") or request_id,
                    "request_method": method,
                    "prompt": params.get("prompt") or params.get("message") or params.get("label"),
                    "status": "unsupported",
                },
                "raw_payload": message,
            }
        if not _is_approval_request(method, params):
            if request_id is not None:
                self.app_server.send_response(request_id, error={"code": "unsupported_request", "message": f"unsupported app-server request: {method}"})
            return None
        if request_id is not None:
            self.pending_approval_requests[str(request_id)] = message
        event = self._approval_event_from_request(
            message,
            method,
            params,
            internal_thread_id,
            internal_turn_id,
        )
        if request_id is not None:
            self.pending_approval_events[str(request_id)] = event
        return event

    def _approval_event_from_request(
        self,
        message: JsonDict,
        method: str,
        params: JsonDict,
        internal_thread_id: str | None,
        internal_turn_id: str | None,
    ) -> JsonDict:
        request_id = message.get("id")
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        app_server_approval_id = params.get("approvalId") or params.get("approval_id")
        approval_id = (
            app_server_approval_id
            or item.get("approvalId")
            or item.get("approval_id")
        )
        payload = {
            "request_id": request_id,
            "app_server_request_id": request_id,
            "approval_method": method,
            "command_preview": _command_preview(params),
            "risk_level": params.get("riskLevel") or params.get("risk_level"),
            "available_decisions": _first_present(
                params,
                "availableDecisions",
                "available_decisions",
            ),
        }
        for target_key, source_keys in {
            "item_id": ("itemId", "item_id"),
            "reason": ("reason",),
            "cwd": ("cwd",),
            "permissions": ("permissions",),
            "command_actions": ("commandActions", "command_actions"),
            "additional_permissions": (
                "additionalPermissions",
                "additional_permissions",
            ),
            "network_approval_context": (
                "networkApprovalContext",
                "network_approval_context",
            ),
            "proposed_execpolicy_amendment": (
                "proposedExecpolicyAmendment",
                "proposed_execpolicy_amendment",
            ),
            "proposed_network_policy_amendments": (
                "proposedNetworkPolicyAmendments",
                "proposed_network_policy_amendments",
            ),
            "grant_root": ("grantRoot", "grant_root"),
        }.items():
            value = _first_present(params, *source_keys)
            if value is not None:
                payload[target_key] = value
        if approval_id is not None:
            payload["approval_id"] = approval_id
        if app_server_approval_id is not None:
            payload["app_server_approval_id"] = app_server_approval_id
        return {
            "type": "app_server.request_opened",
            "kind": "approval",
            "method": method,
            "thread_id": internal_thread_id,
            "turn_id": internal_turn_id,
            "app_server_thread_id": _app_thread_id_from_params(params),
            "app_server_turn_id": _app_turn_id_from_params(params),
            "app_server_request_id": request_id,
            "payload": payload,
            "raw_payload": message,
        }

    def _flush_unmapped_server_requests(self) -> None:
        if not self.pending_unmapped_server_requests:
            return
        for key, message in list(self.pending_unmapped_server_requests.items()):
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if not isinstance(method, str) or not _is_approval_request(method, params):
                self.pending_unmapped_server_requests.pop(key, None)
                continue
            app_thread_id = _app_thread_id_from_params(params)
            internal_thread_id = self.reverse_thread_map.get(app_thread_id)
            if not internal_thread_id:
                continue
            app_turn_id = _app_turn_id_from_params(params)
            internal_turn_id = self.reverse_turn_map.get(app_turn_id)
            event = self._approval_event_from_request(
                message,
                method,
                params,
                internal_thread_id,
                internal_turn_id,
            )
            self.pending_unmapped_server_requests.pop(key, None)
            self.pending_approval_events[str(message.get("id"))] = event
            self._send_backend(event)

    def replay_pending_approvals(self) -> None:
        self._flush_unmapped_server_requests()

    def _normalize_notification(self, message: JsonDict, method: str, params: JsonDict) -> JsonDict | None:
        if method == "fs/changed":
            watch_id = params.get("watchId") or params.get("watch_id")
            watch = self.fs_watch_map.get(str(watch_id)) if watch_id is not None else None
            internal_thread_id = watch.get("thread_id") if isinstance(watch, dict) else None
            if not internal_thread_id:
                return None
            event_type, payload = normalize_appserver_notification(method, params)
            if event_type is None:
                return None
            payload["path"] = watch.get("path")
            return {
                "type": "app_server.event",
                "event_type": event_type,
                "thread_id": internal_thread_id,
                "turn_id": None,
                "payload": payload,
                "raw_payload": message,
            }
        app_thread_id = _app_thread_id_from_params(params)
        internal_thread_id = self.reverse_thread_map.get(app_thread_id)
        app_turn_id = _app_turn_id_from_params(params)
        internal_turn_id = self.reverse_turn_map.get(app_turn_id)
        if method == "thread/started":
            thread = params.get("thread") or {}
            app_thread_id = thread.get("id") or app_thread_id
            internal_thread_id = self.reverse_thread_map.get(app_thread_id)
        if not internal_thread_id:
            return None
        event_type, payload = normalize_appserver_notification(method, params)
        if event_type is None:
            return None
        if event_type == "assistant.delta":
            item_id = payload.get("item_id")
            self._append_assistant_delta(
                internal_thread_id,
                internal_turn_id,
                item_id if isinstance(item_id, str) else None,
                payload.get("text"),
            )
        elif event_type == "assistant.message":
            item_id = payload.get("item_id") if isinstance(payload.get("item_id"), str) else None
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                text = self._pop_assistant_delta(
                    internal_thread_id,
                    internal_turn_id,
                    item_id,
                )
            else:
                self._pop_assistant_delta(
                    internal_thread_id,
                    internal_turn_id,
                    item_id,
                )
            payload = {**payload, "text": text or ""}
        elif event_type == "item.completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "agentMessage":
                item_id = item.get("id") if isinstance(item.get("id"), str) else None
                text = item.get("text")
                if not isinstance(text, str) or not text:
                    text = self._pop_assistant_delta(
                        internal_thread_id,
                        internal_turn_id,
                        item_id,
                    )
                else:
                    self._pop_assistant_delta(
                        internal_thread_id,
                        internal_turn_id,
                        item_id,
                    )
                if text:
                    event_type = "assistant.message"
                    payload = {
                        "text": text,
                        "item_id": item_id,
                        "status": item.get("status") or "completed",
                        "phase": _agent_message_phase(item.get("phase")),
                    }
        if app_thread_id:
            payload["app_server_thread_id"] = app_thread_id
        if app_turn_id:
            payload["app_server_turn_id"] = app_turn_id
        if event_type == "approval.resolved":
            request_id = _first_present(
                payload,
                "app_server_request_id",
                "request_id",
            )
            if request_id is not None:
                self.pending_approval_requests.pop(str(request_id), None)
                self.pending_approval_events.pop(str(request_id), None)
                self.pending_unmapped_server_requests.pop(str(request_id), None)
        if method == "thread/closed" and app_thread_id:
            self.reverse_thread_map.pop(app_thread_id, None)
            if internal_thread_id:
                self.thread_map.pop(internal_thread_id, None)
        return {
            "type": "app_server.event",
            "event_type": event_type,
            "thread_id": internal_thread_id,
            "turn_id": internal_turn_id,
            "payload": payload,
            "raw_payload": message,
        }

    def _append_assistant_delta(
        self,
        thread_id: str,
        turn_id: str | None,
        item_id: str | None,
        text: Any,
    ) -> None:
        if not isinstance(text, str) or not text:
            return
        self.assistant_delta_buffers.setdefault((thread_id, turn_id, item_id), []).append(text)

    def _pop_assistant_delta(
        self,
        thread_id: str,
        turn_id: str | None,
        item_id: str | None,
    ) -> str:
        keys = [(thread_id, turn_id, item_id)]
        if item_id is not None:
            keys.append((thread_id, turn_id, None))
        for key in list(self.assistant_delta_buffers):
            if key[0] == thread_id and key[1] == turn_id and key not in keys:
                keys.append(key)
        for key in keys:
            parts = self.assistant_delta_buffers.pop(key, None)
            if parts:
                return "".join(parts)
        return ""

    def _send_backend_event(self, event_type: str, thread_id: str, turn_id: str | None, payload: JsonDict, raw_payload: Any = None) -> None:
        self._send_backend(
            {
                "type": "app_server.event",
                "event_type": event_type,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "payload": payload,
                "raw_payload": raw_payload,
            }
        )

    def _send_backend(self, message: JsonDict) -> None:
        if self.backend_send is None:
            return
        result = self.backend_send(message)
        if asyncio.iscoroutine(result):
            logger.warning("async backend sender result ignored; must be scheduled by caller")

    @staticmethod
    def _ok(request_id: Any, payload: JsonDict | None = None) -> JsonDict:
        return {"type": "connector.response", "request_id": request_id, "status": "ok", "payload": payload or {}}

    @staticmethod
    def _error(request_id: Any, code: str, message: str) -> JsonDict:
        return {
            "type": "connector.response",
            "request_id": request_id,
            "status": "error",
            "error": {"code": code, "message": message},
        }


# ---------------------------------------------------------------------------
# Notification normalizers
# ---------------------------------------------------------------------------

def normalize_appserver_notification(method: str, params: JsonDict) -> tuple[str | None, JsonDict]:
    if method == "thread/status/changed":
        return "thread.status", {"status": _thread_status(params.get("status"))}
    if method == "thread/name/updated":
        return "thread.status", {"title": params.get("name") or params.get("title")}
    if method == "thread/closed":
        return "thread.status", {"status": "idle", "closed": True}
    if method == "fs/changed":
        changed_paths = params.get("changedPaths") or params.get("changed_paths") or []
        if not isinstance(changed_paths, list):
            changed_paths = []
        return "file.changed", {
            "watch_id": params.get("watchId") or params.get("watch_id"),
            "paths": [_string_value(path) for path in changed_paths if _string_value(path)],
            "status": "changed",
        }
    if method == "turn/started":
        turn = params.get("turn") or {}
        return "turn.started", {"status": turn.get("status") or "inProgress"}
    if method == "turn/completed":
        turn = params.get("turn") or {}
        return "turn.completed", {"status": turn.get("status") or "completed", "error": turn.get("error")}
    if method == "item/agentMessage/delta":
        return "assistant.delta", {
            "item_id": params.get("itemId") or params.get("item_id"),
            "text": params.get("delta") or params.get("text") or "",
        }
    if method == "item/commandExecution/outputDelta":
        return "command.output", {
            "item_id": params.get("itemId") or params.get("item_id"),
            "text": params.get("delta") or params.get("text") or "",
        }
    if method in {"turn/plan/updated", "plan/updated"}:
        plan = _normalize_plan_steps(params.get("plan"))
        text = params.get("text") or params.get("summary")
        if not plan and isinstance(params.get("plan"), str):
            text = text or params.get("plan")
        return "plan.updated", {
            "explanation": params.get("explanation") or params.get("summary") or "",
            "plan": plan,
            **({"text": text} if isinstance(text, str) and text else {}),
        }
    if method in {"turn/plan/delta", "plan/delta"}:
        return "plan.delta", {
            "text": params.get("delta") or params.get("text") or "",
        }
    if method in {"turn/reasoning/delta", "item/reasoning/delta", "reasoning/delta"}:
        return "reasoning.delta", {
            "item_id": params.get("itemId") or params.get("item_id"),
            "text": params.get("delta") or params.get("text") or "",
        }
    if method in {"turn/reasoning/summary", "reasoning/summary"}:
        return "reasoning.summary", {
            "text": params.get("summary") or params.get("text") or "",
        }
    if method == "item/started":
        item = params.get("item") or {}
        small = _small_item(item)
        if item.get("type") == "commandExecution":
            return "command.started", {
                "item_id": small.get("id"),
                "command": small.get("command"),
                "status": small.get("status") or "running",
            }
        return "item.started", {"item": small}
    if method == "item/completed":
        item = params.get("item") or {}
        small = _small_item(item)
        if item.get("type") == "commandExecution":
            return "command.completed", {
                "item_id": small.get("id"),
                "command": small.get("command"),
                "text": item.get("aggregatedOutput") or item.get("output") or "",
                "status": small.get("status") or "completed",
                "exit_code": small.get("exit_code") or small.get("exitCode"),
            }
        if item.get("type") == "agentMessage":
            return "assistant.message", {
                "text": item.get("text") or "",
                "item_id": small.get("id"),
                "status": small.get("status") or "completed",
                "phase": _agent_message_phase(item.get("phase")),
            }
        if item.get("type") in {"fileChange", "fileEdit"}:
            return "file.changed", {
                "item_id": small.get("id"),
                "path": small.get("path"),
                "change_type": small.get("change_type"),
                "changes": small.get("changes") or [],
                "status": small.get("status"),
            }
        return "item.completed", {"item": small}
    if method == "error":
        return "turn.failed", {
            "error": params.get("error") or {},
            "will_retry": params.get("willRetry"),
        }
    if method == "serverRequest/resolved":
        request_id = _first_present(params, "requestId", "id")
        return "approval.resolved", {
            "request_id": request_id,
            "app_server_request_id": request_id,
        }
    return "app_server.notification", {"method": method, "params": params}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_approval_request(method: str, params: JsonDict) -> bool:
    if "approval" in method.lower():
        return True
    if params.get("approvalId") or params.get("availableDecisions"):
        return True
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    return bool(item.get("approvalId") or item.get("approval_id"))


def _is_user_input_request(method: str) -> bool:
    lowered = method.lower()
    return "requestuserinput" in lowered or lowered.endswith("/request-user-input")


def _is_mcp_elicitation_request(method: str) -> bool:
    return method == "mcpServer/elicitation/request"


def _is_auth_refresh_request(method: str) -> bool:
    return method == "account/chatgptAuthTokens/refresh"


def _safe_account(value: Any) -> JsonDict | None:
    if not isinstance(value, dict):
        return None
    account_type = _string_value(value.get("type"))
    if account_type == "chatgpt":
        return {
            "type": "chatgpt",
            "email": _string_value(value.get("email")),
            "plan_type": _string_value(_first_present(value, "planType", "plan_type")),
        }
    if account_type == "apiKey":
        return {"type": "apiKey"}
    if account_type == "amazonBedrock":
        return {"type": "amazonBedrock"}
    return {"type": account_type or "unknown"}


def _safe_rate_limits(value: Any) -> tuple[JsonDict | None, dict[str, JsonDict] | None]:
    if not isinstance(value, dict):
        return None, None
    primary = _safe_rate_limit_snapshot(_first_present(value, "rateLimits", "rate_limits"))
    raw_by_id = _first_present(value, "rateLimitsByLimitId", "rate_limits_by_limit_id")
    by_id: dict[str, JsonDict] = {}
    if isinstance(raw_by_id, dict):
        for key, item in raw_by_id.items():
            snapshot = _safe_rate_limit_snapshot(item)
            if snapshot is not None:
                by_id[str(key)] = snapshot
    return primary, by_id or None


def _safe_rate_limit_snapshot(value: Any) -> JsonDict | None:
    if not isinstance(value, dict):
        return None
    return {
        "limit_id": _string_value(value.get("limitId") or value.get("limit_id")),
        "limit_name": _string_value(value.get("limitName") or value.get("limit_name")),
        "primary": _safe_rate_limit_window(value.get("primary")),
        "secondary": _safe_rate_limit_window(value.get("secondary")),
        "credits": _safe_credits(value.get("credits")),
        "plan_type": _string_value(_first_present(value, "planType", "plan_type")),
        "rate_limit_reached_type": _string_value(
            _first_present(value, "rateLimitReachedType", "rate_limit_reached_type")
        ),
    }


def _safe_rate_limit_window(value: Any) -> JsonDict | None:
    if not isinstance(value, dict):
        return None
    return {
        "used_percent": _number_value(_first_present(value, "usedPercent", "used_percent")),
        "window_duration_mins": _number_value(
            _first_present(value, "windowDurationMins", "window_duration_mins")
        ),
        "resets_at": _number_value(_first_present(value, "resetsAt", "resets_at")),
    }


def _safe_credits(value: Any) -> JsonDict | None:
    if not isinstance(value, dict):
        return None
    return {
        "has_credits": _bool_value(_first_present(value, "hasCredits", "has_credits")),
        "unlimited": _bool_value(value.get("unlimited")) or False,
        "balance": _string_value(value.get("balance")),
    }


def _number_value(value: Any) -> int | float | None:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _first_present(value: JsonDict, *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value.get(key)
    return None


def _extract_thread_list(result: JsonDict) -> list[JsonDict]:
    raw = result.get("data") or result.get("threads") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _normalize_thread_for_sync(thread: JsonDict) -> JsonDict:
    app_thread_id = _string_value(thread.get("id") or thread.get("threadId"))
    git_info = thread.get("gitInfo") if isinstance(thread.get("gitInfo"), dict) else {}
    return {
        "app_server_thread_id": app_thread_id,
        "title": _string_value(thread.get("name")) or _preview_text(thread.get("preview")),
        "preview": _preview_text(thread.get("preview")),
        "status": _thread_status(thread.get("status")),
        "remote_path": _thread_remote_path(thread),
        "current_branch": _string_value(git_info.get("branch") or git_info.get("currentBranch")),
        "created_at": _timestamp_seconds(thread.get("createdAt")),
        "updated_at": _timestamp_seconds(thread.get("UpdatedAt")) or _timestamp_seconds(thread.get("updatedAt")),
    }


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _app_thread_id_from_params(params: JsonDict) -> str | None:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    thread = params.get("thread") if isinstance(params.get("thread"), dict) else {}
    return _string_value(
        params.get("threadId")
        or params.get("thread_id")
        or thread.get("id")
        or thread.get("threadId")
        or thread.get("thread_id")
        or item.get("threadId")
        or item.get("thread_id")
    )


def _app_turn_id_from_params(params: JsonDict) -> str | None:
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
    return _string_value(
        params.get("turnId")
        or params.get("turn_id")
        or turn.get("id")
        or turn.get("turnId")
        or turn.get("turn_id")
        or item.get("turnId")
        or item.get("turn_id")
    )


def _agent_message_phase(value: Any) -> str:
    return "commentary" if value == "commentary" else "final_answer"


def _normalize_plan_steps(value: Any) -> list[JsonDict]:
    if isinstance(value, dict):
        value = value.get("plan") or value.get("steps") or value.get("items")
    if not isinstance(value, list):
        return []
    steps: list[JsonDict] = []
    for item in value:
        if isinstance(item, dict):
            step = _string_value(
                item.get("step") or item.get("text") or item.get("title")
            )
            if step:
                steps.append({"step": step, "status": item.get("status")})
        else:
            step = _string_value(item)
            if step:
                steps.append({"step": step, "status": None})
    return steps


def _thread_remote_path(thread: JsonDict) -> str | None:
    for key in ("cwd", "path", "remotePath", "workspace", "worktree", "root", "directory"):
        value = _path_value(thread.get(key))
        if value:
            return value
    return None


def _path_value(value: Any) -> str | None:
    direct = _string_value(value)
    if direct:
        return direct
    if isinstance(value, dict):
        for key in ("path", "value", "cwd", "remotePath", "root", "directory", "workspace", "worktree"):
            nested = _path_value(value.get(key))
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _path_value(item)
            if nested:
                return nested
    return None


def _preview_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "summary", "content"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
    return None


def _timestamp_seconds(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 10_000_000_000 else number
    if isinstance(value, str):
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def _thread_status(status: Any) -> str:
    if isinstance(status, dict):
        status_type = status.get("type")
        flags = status.get("activeFlags") or []
        if status_type == "active" and "waitingOnApproval" in flags:
            return "needs_approval"
        if status_type == "active" and "waitingOnUserInput" in flags:
            return "needs_user_input"
        if status_type == "active":
            return "running"
        if status_type == "systemError":
            return "failed"
        if status_type == "idle":
            return "idle"
        if status_type == "archived":
            return "archived"
        return "running"
    if isinstance(status, str) and status in {"idle", "running", "needs_approval", "needs_user_input", "failed", "archived"}:
        return status
    return "running"


def _small_item(item: JsonDict) -> JsonDict:
    result = {
        "id": item.get("id"),
        "type": item.get("type"),
        "status": item.get("status"),
        "command": _command_preview(item),
        "path": _path_value(item.get("path") or item.get("filePath") or item.get("absolutePath")),
        "change_type": item.get("changeType") or item.get("change_type"),
        "exitCode": item.get("exitCode"),
        "exit_code": item.get("exit_code"),
    }
    if item.get("type") == "agentMessage":
        result["text"] = item.get("text")
    if item.get("type") in {"fileChange", "fileEdit"}:
        changes = _small_file_changes(item.get("changes"))
        if changes:
            result["changes"] = changes
            if not result.get("path"):
                result["path"] = changes[0].get("path")
            if not result.get("change_type"):
                result["change_type"] = changes[0].get("change_type")
    return result


def _small_file_changes(value: Any) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    changes: list[JsonDict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = _path_value(item.get("path") or item.get("filePath") or item.get("absolutePath"))
        if not path:
            continue
        kind = item.get("kind")
        change_type = item.get("changeType") or item.get("change_type")
        if not change_type and isinstance(kind, dict):
            change_type = kind.get("type")
        elif not change_type and isinstance(kind, str):
            change_type = kind
        small: JsonDict = {"path": path}
        if change_type:
            small["change_type"] = change_type
        diff = item.get("diff") or item.get("unified_diff")
        if isinstance(diff, str) and diff:
            small["diff"] = diff[:8192]
        changes.append(small)
    return changes


def _command_preview(params: JsonDict) -> str | None:
    command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    if isinstance(item.get("command"), str):
        return item.get("command")
    return None


def _sandbox_policy(cwd: str) -> JsonDict:
    return {
        "type": "workspaceWrite",
        "writableRoots": [cwd],
        "permissionProfile": "restricted",
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _git_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    branch = result.stdout.strip()
    return branch or None
