from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass

from agent_connector.cli import (
    ClaudeCodeConnector,
    ConnectionSpec,
    _connection_args,
    envelope_to_backend_message,
    provider_hello,
    reconnect_command,
    resolve_connection_specs,
)
from agent_connector.providers.claude_agent_sdk import (
    ClaudeAgentSdkProvider,
    ClaudeAgentSdkRuntimeMissing,
)
from agent_connector.providers.codex_app_server import AppServerProcessClient
from agent_connector.token_store import (
    DEFAULT_SERVER,
    load_saved_connectors,
    save_connector_token,
)


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-sonnet"
    message_id: str = "msg_1"


def _request() -> dict:
    return {
        "thread_id": "thread_1",
        "turn_id": "turn_1",
        "provider_session_id": "session_old",
    }


def test_claude_message_mapping_uses_unified_event_shape() -> None:
    provider = ClaudeAgentSdkProvider(cwd=".")
    message = AssistantMessage(
        content=[
            TextBlock("done"),
            ToolUseBlock("tool_1", "Bash", {"command": "pwd"}),
        ],
    )

    envelopes = provider.map_message(_request(), message)

    assert [item.type for item in envelopes] == [
        "assistant.message",
        "command.started",
    ]
    assert envelopes[0].payload["phase"] == "final_answer"
    assert envelopes[1].payload["command"] == "pwd"


def test_hello_and_backend_event_shape_are_provider_neutral() -> None:
    provider = ClaudeAgentSdkProvider(cwd=".")
    hello = provider_hello(provider, connector_version="test")
    assert hello["type"] == "connector.hello"
    assert hello["provider"] == "claude_code"
    assert "app_server.turn_start" in hello["capabilities"]

    envelope = provider._envelope(
        "assistant.message",
        _request(),
        {"text": "hello", "phase": "final_answer"},
        provider_event_id="event_1",
    )
    message = envelope_to_backend_message(envelope, _request(), request_id="req_1")
    assert message["type"] == "app_server.event"
    assert message["event_type"] == "assistant.message"
    assert message["payload"]["provider"] == "claude_code"
    assert message["payload"]["provider_event_id"] == "event_1"


def test_reconnect_command_does_not_include_provider() -> None:
    class Args:
        server = "https://www.botsdock.cn"
        cwd = "."
        model = None

    command = reconnect_command(Args())

    assert command == "botsdock-agent-connector"
    assert "--provider" not in command


def test_saved_connectors_can_be_loaded_together() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        save_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_codex",
            token="token_codex",
            provider="codex",
            cwd=tmp,
        )
        save_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_claude",
            token="token_claude",
            provider="claude_code",
            cwd=tmp,
        )

        connectors = load_saved_connectors(server_url=DEFAULT_SERVER, cwd=tmp)

    assert sorted((item.machine_id, item.token, item.provider) for item in connectors) == [
        ("mach_claude", "token_claude", "claude_code"),
        ("mach_codex", "token_codex", "codex"),
    ]


def test_no_arg_connection_resolution_supervises_all_saved_connectors() -> None:
    class Args:
        server = DEFAULT_SERVER
        machine_id = None
        token = None
        cwd = ""

    with tempfile.TemporaryDirectory() as tmp:
        Args.cwd = tmp
        save_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_codex",
            token="token_codex",
            provider="codex",
            cwd=tmp,
        )
        save_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_claude",
            token="token_claude",
            provider="claude_code",
            cwd=tmp,
        )

        specs = resolve_connection_specs(Args())

    assert sorted((item.machine_id, item.provider) for item in specs) == [
        ("mach_claude", "claude_code"),
        ("mach_codex", "codex"),
    ]


def test_registration_connection_args_exit_after_token_exchange() -> None:
    class Args:
        server = DEFAULT_SERVER
        machine_id = "mach_new"
        token = "registration_token"
        cwd = "."

    spec = ConnectionSpec(
        server=DEFAULT_SERVER,
        machine_id="mach_new",
        token="registration_token",
        cwd=".",
    )

    connection_args = _connection_args(Args(), spec)

    assert connection_args.registration_only is True


def test_claude_thread_history_returns_empty_history_shape() -> None:
    async def run() -> dict:
        provider = ClaudeAgentSdkProvider(cwd=".")
        connector = ClaudeCodeConnector(provider=provider, outbound=asyncio.Queue())
        response = await connector.handle_backend_message(
            {
                "type": "connector.thread_history",
                "request_id": "req_1",
                "payload": {
                    "thread_id": "thread_1",
                    "direction": "latest",
                },
            }
        )
        assert response is not None
        return response

    response = asyncio.run(run())

    assert response["status"] == "ok"
    assert response["payload"]["type"] == "thread.history"
    assert response["payload"]["turns"] == []
    assert response["payload"]["has_more_before"] is False


def test_missing_claude_sdk_is_reported_as_turn_failed() -> None:
    provider = ClaudeAgentSdkProvider(cwd=".")

    async def missing_sdk() -> None:
        raise ClaudeAgentSdkRuntimeMissing("claude_agent_sdk is not installed")

    async def collect() -> list:
        provider.start = missing_sdk  # type: ignore[method-assign]
        events = []
        async for event in provider.start_turn({**_request(), "prompt": "hello"}):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert [event.type for event in events] == ["turn.failed"]
    assert events[0].payload["error"] == "provider_runtime_missing"


def test_codex_app_server_initializes_experimental_api_capability() -> None:
    class FakeAppServer(AppServerProcessClient):
        def __init__(self) -> None:
            self.requests = []
            self.notifications = []

        def request(self, method, params=None):
            self.requests.append((method, params))
            return {"ok": True}

        def notification(self, method, params=None) -> None:
            self.notifications.append((method, params))

    app_server = FakeAppServer()

    result = app_server.initialize()

    assert result == {"ok": True}
    method, params = app_server.requests[0]
    assert method == "initialize"
    assert params["capabilities"]["experimentalApi"] is True
    assert app_server.notifications == [("initialized", None)]
