from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .base import AgentProviderCapabilities, JsonDict, ProviderEnvelope


_AUTO_ALLOW_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "LS",
    "TodoRead",
)
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_BASH_TOOL = "Bash"


class ClaudeAgentSdkRuntimeMissing(RuntimeError):
    pass


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _payload_value(request: JsonDict, key: str) -> Any:
    if key in request:
        return request.get(key)
    payload = request.get("payload")
    if isinstance(payload, dict):
        return payload.get(key)
    return None


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {
            str(k): _jsonable(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _fingerprint(tool_name: str, input_data: JsonDict) -> str:
    try:
        body = json.dumps(input_data, sort_keys=True, separators=(",", ":"))
    except TypeError:
        body = json.dumps(_jsonable(input_data), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{tool_name}\n{body}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _approval_decision_allows(response: JsonDict | None) -> bool:
    if not isinstance(response, dict):
        return False
    decision = str(response.get("decision") or response.get("status") or "").lower()
    if decision in {"approved", "approve", "allow", "allowed", "yes"}:
        return True
    app_server_decision = response.get("app_server_decision")
    if isinstance(app_server_decision, dict):
        decision = str(
            app_server_decision.get("decision")
            or app_server_decision.get("status")
            or app_server_decision.get("behavior")
            or ""
        ).lower()
        return decision in {"approved", "approve", "allow", "allowed", "yes"}
    return False


class ClaudeAgentSdkProvider:
    name = "claude_code"
    capabilities = AgentProviderCapabilities(
        can_resume_session=True,
        can_cancel_turn=True,
        can_request_approval=True,
        can_report_file_activity=True,
        event_types=(
            "assistant.message",
            "tool.started",
            "tool.completed",
            "file.changed",
            "approval.requested",
            "approval.resolved",
            "reasoning.delta",
            "command.started",
            "command.output",
            "command.completed",
            "provider.warning",
            "provider.debug",
            "turn.completed",
            "turn.failed",
            "turn.cancelled",
        ),
    )

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None = None,
        approval_timeout_seconds: float = 900,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.approval_timeout_seconds = approval_timeout_seconds
        self._sdk: Any | None = None
        self._sdk_types: Any | None = None
        self._active_clients: dict[str, Any] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._pending_approvals: dict[str, asyncio.Future[JsonDict]] = {}

    async def start(self) -> None:
        # The SDK import is intentionally lazy so Codex-only deployments do not
        # need Claude dependencies installed.
        try:
            import claude_agent_sdk  # type: ignore
            import claude_agent_sdk.types as claude_agent_sdk_types  # type: ignore
        except ImportError as err:
            raise ClaudeAgentSdkRuntimeMissing(
                "claude_agent_sdk is not installed. Install the optional "
                "claude dependency with `pip install claude-agent-sdk`."
            ) from err
        self._sdk = claude_agent_sdk
        self._sdk_types = claude_agent_sdk_types

    async def stop(self) -> None:
        for client in list(self._active_clients.values()):
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                try:
                    await disconnect()
                except Exception:
                    pass
        self._active_clients.clear()
        self._cancel_events.clear()
        self._sdk = None
        self._sdk_types = None

    async def start_turn(self, request: JsonDict) -> AsyncIterator[ProviderEnvelope]:
        if self._sdk is None:
            try:
                await self.start()
            except Exception as err:
                yield self._turn_failed(
                    request,
                    code=self._error_code(err),
                    message=str(err) or type(err).__name__,
                    raw_event={"error_type": type(err).__name__},
                )
                return
        assert self._sdk is not None
        assert self._sdk_types is not None

        prompt = _string(_payload_value(request, "prompt"))
        if not prompt:
            yield self._turn_failed(
                request,
                code="invalid_request",
                message="prompt is required",
            )
            return

        turn_id = _string(_payload_value(request, "turn_id")) or uuid.uuid4().hex
        cancel_event = asyncio.Event()
        self._cancel_events[turn_id] = cancel_event
        queue: asyncio.Queue[ProviderEnvelope | None] = asyncio.Queue()
        options = self._build_options(
            request,
            can_use_tool=self._build_permission_handler(request, queue),
        )

        yield self._envelope(
            "turn.started",
            request,
            {
                "provider": self.name,
                "cwd": str(self._cwd_for_request(request)),
            },
        )

        async def run_sdk() -> None:
            client = None
            try:
                client = self._sdk.ClaudeSDKClient(options=options)
                self._active_clients[turn_id] = client
                async with client:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        if cancel_event.is_set():
                            break
                        for envelope in self.map_message(request, message):
                            await queue.put(envelope)
                if cancel_event.is_set():
                    await queue.put(
                        self._envelope(
                            "turn.cancelled",
                            request,
                            {"status": "cancelled", "provider": self.name},
                        )
                    )
            except Exception as err:
                await queue.put(
                    self._turn_failed(
                        request,
                        code=self._error_code(err),
                        message=str(err) or type(err).__name__,
                        raw_event={"error_type": type(err).__name__},
                    )
                )
            finally:
                if client is not None:
                    self._active_clients.pop(turn_id, None)
                self._cancel_events.pop(turn_id, None)
                await queue.put(None)

        task = asyncio.create_task(run_sdk())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
            await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def cancel_turn(self, request: JsonDict) -> ProviderEnvelope:
        turn_id = _string(_payload_value(request, "turn_id"))
        if not turn_id:
            return self._turn_failed(
                request,
                code="invalid_request",
                message="turn_id is required for cancellation",
            )
        cancel_event = self._cancel_events.get(turn_id)
        if cancel_event is not None:
            cancel_event.set()
        client = self._active_clients.get(turn_id)
        interrupt = getattr(client, "interrupt", None)
        if callable(interrupt):
            try:
                await interrupt()
            except Exception:
                pass
        return self._envelope(
            "turn.cancelled",
            request,
            {"status": "cancelled", "provider": self.name},
        )

    async def resolve_approval(self, request: JsonDict) -> ProviderEnvelope:
        app_server_request_id = (
            _string(_payload_value(request, "app_server_request_id"))
            or _string(_payload_value(request, "request_id"))
            or _string(_payload_value(request, "provider_request_id"))
        )
        if app_server_request_id:
            future = self._pending_approvals.get(app_server_request_id)
            if future is not None and not future.done():
                future.set_result(dict(request.get("payload") or request))
        return self._envelope(
            "approval.resolved",
            request,
            {
                "provider": self.name,
                "request_id": app_server_request_id,
                "app_server_request_id": app_server_request_id,
                "provider_request_id": app_server_request_id,
                "decision": _payload_value(request, "decision"),
                "request_fingerprint": _payload_value(request, "request_fingerprint"),
            },
        )

    def map_message(self, request: JsonDict, message: Any) -> list[ProviderEnvelope]:
        message_type = type(message).__name__
        raw = _jsonable(message)
        if message_type == "AssistantMessage":
            return self._map_assistant_message(request, message, raw)
        if message_type == "ResultMessage":
            return [self._map_result_message(request, message, raw)]
        if message_type == "StreamEvent":
            envelope = self._map_stream_event(request, message, raw)
            return [envelope] if envelope is not None else []
        if message_type == "RateLimitEvent":
            return [
                self._envelope(
                    "provider.warning",
                    request,
                    {
                        "provider": self.name,
                        "kind": "rate_limit",
                        "rate_limit": raw.get("rate_limit_info", raw),
                    },
                    raw_event=raw,
                    provider_event_id=_string(raw.get("uuid")),
                    provider_session_id=_string(raw.get("session_id")),
                )
            ]
        return []

    def _build_options(
        self,
        request: JsonDict,
        *,
        can_use_tool: Callable[[str, JsonDict, Any], Any] | None,
    ) -> Any:
        assert self._sdk is not None
        cwd = self._cwd_for_request(request)
        model = _string(_payload_value(request, "model")) or self.model
        reasoning_effort = _string(_payload_value(request, "reasoning_effort"))
        provider_session_id = _string(_payload_value(request, "provider_session_id"))
        approval_policy = _string(_payload_value(request, "approval_policy"))
        permission_mode = "dontAsk" if approval_policy == "never" else "default"
        kwargs: JsonDict = {
            "cwd": str(cwd),
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "tools": {"type": "preset", "preset": "claude_code"},
            "allowed_tools": list(_AUTO_ALLOW_TOOLS),
            "permission_mode": permission_mode,
            "setting_sources": ["project", "local"],
            "can_use_tool": None if permission_mode == "dontAsk" else can_use_tool,
        }
        if model:
            kwargs["model"] = model
        if provider_session_id:
            kwargs["resume"] = provider_session_id
        if reasoning_effort in {"low", "medium", "high", "max"}:
            kwargs["effort"] = reasoning_effort
        return self._sdk.ClaudeAgentOptions(**kwargs)

    def _build_permission_handler(
        self,
        request: JsonDict,
        queue: asyncio.Queue[ProviderEnvelope | None],
    ) -> Callable[[str, JsonDict, Any], Any]:
        async def can_use_tool(tool_name: str, input_data: JsonDict, context: Any) -> Any:
            assert self._sdk_types is not None
            if tool_name in _AUTO_ALLOW_TOOLS:
                return self._sdk_types.PermissionResultAllow()

            provider_request_id = f"claude-perm-{uuid.uuid4().hex}"
            request_fingerprint = _fingerprint(tool_name, input_data)
            future = asyncio.get_running_loop().create_future()
            self._pending_approvals[provider_request_id] = future
            payload = {
                "provider": self.name,
                "request_id": provider_request_id,
                "app_server_request_id": provider_request_id,
                "provider_request_id": provider_request_id,
                "request_fingerprint": request_fingerprint,
                "title": f"Claude Code wants to use {tool_name}",
                "kind": "tool",
                "tool_name": tool_name,
                "input": _jsonable(input_data),
                "tool_permission_context": _jsonable(context),
            }
            command = input_data.get("command") if isinstance(input_data, dict) else None
            if isinstance(command, str) and command:
                payload["command"] = command
                payload["kind"] = "command"
            await queue.put(self._envelope("approval.requested", request, payload))
            try:
                response = await asyncio.wait_for(
                    future,
                    timeout=self.approval_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return self._sdk_types.PermissionResultDeny(
                    message="Approval request timed out",
                    interrupt=True,
                )
            finally:
                self._pending_approvals.pop(provider_request_id, None)

            if _approval_decision_allows(response):
                return self._sdk_types.PermissionResultAllow()
            return self._sdk_types.PermissionResultDeny(
                message=_string(response.get("message")) or "Denied by user",
                interrupt=True,
            )

        return can_use_tool

    def _map_assistant_message(
        self,
        request: JsonDict,
        message: Any,
        raw: JsonDict,
    ) -> list[ProviderEnvelope]:
        envelopes: list[ProviderEnvelope] = []
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return envelopes
        message_id = _string(getattr(message, "message_id", None))
        model = _string(getattr(message, "model", None))
        for block in content:
            block_type = type(block).__name__
            provider_event_id = _string(getattr(block, "id", None)) or message_id
            if block_type == "TextBlock":
                text = _string(getattr(block, "text", None))
                if text:
                    envelopes.append(
                        self._envelope(
                            "assistant.message",
                            request,
                            {
                                "role": "assistant",
                                "phase": "final_answer",
                                "text": text,
                                "provider": self.name,
                                "model": model,
                                "provider_message_id": message_id,
                            },
                            raw_event=raw,
                            provider_event_id=provider_event_id,
                        )
                    )
            elif block_type == "ThinkingBlock":
                thinking = _string(getattr(block, "thinking", None))
                if thinking:
                    envelopes.append(
                        self._envelope(
                            "reasoning.delta",
                            request,
                            {
                                "text": thinking,
                                "provider": self.name,
                                "provider_message_id": message_id,
                            },
                            raw_event=raw,
                            provider_event_id=provider_event_id,
                        )
                    )
            elif block_type == "ToolUseBlock":
                envelopes.extend(
                    self._map_tool_use(request, block, raw, provider_event_id)
                )
            elif block_type == "ToolResultBlock":
                envelopes.extend(self._map_tool_result(request, block, raw))
        return envelopes

    def _map_tool_use(
        self,
        request: JsonDict,
        block: Any,
        raw: JsonDict,
        provider_event_id: str | None,
    ) -> list[ProviderEnvelope]:
        tool_name = _string(getattr(block, "name", None)) or "tool"
        item_id = _string(getattr(block, "id", None)) or provider_event_id
        input_data = getattr(block, "input", None)
        input_payload = input_data if isinstance(input_data, dict) else {}
        if tool_name == _BASH_TOOL:
            command = _string(input_payload.get("command")) or tool_name
            return [
                self._envelope(
                    "command.started",
                    request,
                    {
                        "item_id": item_id,
                        "command": command,
                        "status": "running",
                        "provider": self.name,
                        "tool_name": tool_name,
                        "input": _jsonable(input_payload),
                    },
                    raw_event=raw,
                    provider_event_id=provider_event_id,
                )
            ]
        if tool_name in _EDIT_TOOLS:
            path = _string(
                input_payload.get("file_path")
                or input_payload.get("path")
                or input_payload.get("notebook_path")
            )
            return [
                self._envelope(
                    "file.changed",
                    request,
                    {
                        "item_id": item_id,
                        "path": path,
                        "change_type": tool_name.lower(),
                        "status": "started",
                        "provider": self.name,
                        "tool_name": tool_name,
                        "input": _jsonable(input_payload),
                    },
                    raw_event=raw,
                    provider_event_id=provider_event_id,
                )
            ]
        return [
            self._envelope(
                "provider.debug",
                request,
                {
                    "provider": self.name,
                    "kind": "tool_use",
                    "tool_name": tool_name,
                    "item_id": item_id,
                    "input": _jsonable(input_payload),
                },
                raw_event=raw,
                provider_event_id=provider_event_id,
            )
        ]

    def _map_tool_result(
        self,
        request: JsonDict,
        block: Any,
        raw: JsonDict,
    ) -> list[ProviderEnvelope]:
        tool_use_id = _string(getattr(block, "tool_use_id", None))
        content = getattr(block, "content", None)
        text = content if isinstance(content, str) else json.dumps(_jsonable(content))
        is_error = bool(getattr(block, "is_error", False))
        return [
            self._envelope(
                "command.output",
                request,
                {
                    "item_id": tool_use_id,
                    "text": text or "",
                    "status": "failed" if is_error else "completed",
                    "provider": self.name,
                },
                raw_event=raw,
                provider_event_id=tool_use_id,
            ),
            self._envelope(
                "command.completed",
                request,
                {
                    "item_id": tool_use_id,
                    "status": "failed" if is_error else "completed",
                    "provider": self.name,
                },
                raw_event=raw,
                provider_event_id=tool_use_id,
            ),
        ]

    def _map_result_message(
        self,
        request: JsonDict,
        message: Any,
        raw: JsonDict,
    ) -> ProviderEnvelope:
        session_id = _string(getattr(message, "session_id", None))
        turn_id = _string(_payload_value(request, "turn_id"))
        cancelled = bool(turn_id and self._cancel_events.get(turn_id, asyncio.Event()).is_set())
        is_error = bool(getattr(message, "is_error", False))
        event_type = "turn.cancelled" if cancelled else ("turn.failed" if is_error else "turn.completed")
        payload = {
            "provider": self.name,
            "status": "cancelled" if cancelled else ("failed" if is_error else "completed"),
            "provider_session_id": session_id,
            "provider_thread_id": session_id,
            "provider_turn_id": turn_id,
            "subtype": _string(getattr(message, "subtype", None)),
            "result": _string(getattr(message, "result", None)),
            "stop_reason": _string(getattr(message, "stop_reason", None)),
            "duration_ms": getattr(message, "duration_ms", None),
            "duration_api_ms": getattr(message, "duration_api_ms", None),
            "num_turns": getattr(message, "num_turns", None),
            "total_cost_usd": getattr(message, "total_cost_usd", None),
            "usage": _jsonable(getattr(message, "usage", None)),
            "model_usage": _jsonable(getattr(message, "model_usage", None)),
        }
        if is_error and not payload.get("result"):
            payload["error"] = payload.get("subtype") or "claude_code_error"
        return self._envelope(
            event_type,
            request,
            payload,
            raw_event=raw,
            provider_session_id=session_id,
        )

    def _map_stream_event(
        self,
        request: JsonDict,
        message: Any,
        raw: JsonDict,
    ) -> ProviderEnvelope | None:
        event = getattr(message, "event", None)
        if not isinstance(event, dict):
            return None
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        text = delta.get("text") or delta.get("thinking")
        if not isinstance(text, str) or not text:
            return None
        event_type = "reasoning.delta" if delta.get("thinking") else "assistant.delta"
        return self._envelope(
            event_type,
            request,
            {"text": text, "provider": self.name},
            raw_event=raw,
            provider_event_id=_string(getattr(message, "uuid", None)),
            provider_session_id=_string(getattr(message, "session_id", None)),
        )

    def _turn_failed(
        self,
        request: JsonDict,
        *,
        code: str,
        message: str,
        raw_event: JsonDict | None = None,
    ) -> ProviderEnvelope:
        return self._envelope(
            "turn.failed",
            request,
            {
                "provider": self.name,
                "status": "failed",
                "error": code,
                "message": message,
            },
            raw_event=raw_event,
        )

    def _envelope(
        self,
        event_type: str,
        request: JsonDict,
        payload: JsonDict,
        *,
        raw_event: JsonDict | None = None,
        provider_event_id: str | None = None,
        provider_session_id: str | None = None,
    ) -> ProviderEnvelope:
        provider_turn_id = _string(payload.get("provider_turn_id")) or _string(
            _payload_value(request, "turn_id")
        )
        provider_session_id = (
            provider_session_id
            or _string(payload.get("provider_session_id"))
            or _string(_payload_value(request, "provider_session_id"))
        )
        provider_thread_id = (
            _string(payload.get("provider_thread_id"))
            or provider_session_id
            or _string(_payload_value(request, "provider_thread_id"))
            or _string(_payload_value(request, "app_server_thread_id"))
            or _string(_payload_value(request, "thread_id"))
        )
        enriched = {
            **payload,
            "provider": payload.get("provider") or self.name,
            "provider_turn_id": provider_turn_id,
            "provider_thread_id": provider_thread_id,
            "provider_session_id": provider_session_id,
        }
        return ProviderEnvelope(
            type=event_type,
            payload=enriched,
            raw_event=raw_event,
            provider_event_id=provider_event_id
            or _string(payload.get("provider_event_id")),
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            provider_session_id=provider_session_id,
        )

    def _cwd_for_request(self, request: JsonDict) -> Path:
        return Path(_string(_payload_value(request, "cwd")) or self.cwd).expanduser().resolve()

    @staticmethod
    def _error_code(err: Exception) -> str:
        name = type(err).__name__
        lowered = str(err).lower()
        if name == "CLINotFoundError":
            return "claude_code_cli_not_found"
        if "auth" in lowered or "login" in lowered:
            return "provider_auth_required"
        if name == "ClaudeAgentSdkRuntimeMissing":
            return "provider_runtime_missing"
        return "connector_error"
