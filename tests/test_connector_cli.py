from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from botsdock_connector.cli import (
    ClaudeCodeConnector,
    ConnectionSpec,
    _connection_args,
    approval_envelope_to_request_opened,
    build_parser,
    build_upgrade_pip_args,
    envelope_to_backend_message,
    load_env_file,
    provider_hello,
    reconnect_command,
    resolve_connection_specs,
)
from botsdock_connector.providers.claude_agent_sdk import (
    ClaudeAgentSdkProvider,
    ClaudeAgentSdkRuntimeMissing,
    _approval_decision_allows,
)
from botsdock_connector.providers.codex_app_server import AppServerProcessClient
from botsdock_connector.token_store import (
    DEFAULT_SERVER,
    load_connector_token,
    load_saved_connectors,
    save_connector_token,
    token_store_key,
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


@dataclass
class ResultMessage:
    session_id: str = "session_old"
    is_error: bool = False
    subtype: str | None = None
    result: str | None = None


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


def test_claude_auth_prompt_text_is_not_mapped_as_assistant_message() -> None:
    provider = ClaudeAgentSdkProvider(cwd=".")
    message = AssistantMessage(
        content=[TextBlock("Not logged in · Please run /login")],
    )

    envelopes = provider.map_message(_request(), message)

    assert envelopes == []
    result = provider.map_message(_request(), ResultMessage())
    assert [item.type for item in result] == ["turn.failed"]
    assert result[0].payload["error"] == "provider_auth_required"


def test_env_file_parses_local_provider_credentials_without_global_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "connector.env"
        path.write_text(
            "\n".join(
                [
                    "# local only",
                    "BOTS_TEST_BOTSDOCK_CONNECTOR_ENV=from_file",
                    "export BOTS_TEST_BOTSDOCK_CONNECTOR_EXPORTED='quoted'",
                ]
            ),
            encoding="utf-8",
        )
        os.environ.pop("BOTS_TEST_BOTSDOCK_CONNECTOR_ENV", None)
        os.environ.pop("BOTS_TEST_BOTSDOCK_CONNECTOR_EXPORTED", None)

        loaded = load_env_file(str(path))

        assert loaded == {
            "BOTS_TEST_BOTSDOCK_CONNECTOR_ENV": "from_file",
            "BOTS_TEST_BOTSDOCK_CONNECTOR_EXPORTED": "quoted",
        }
        assert "BOTS_TEST_BOTSDOCK_CONNECTOR_ENV" not in os.environ
        assert "BOTS_TEST_BOTSDOCK_CONNECTOR_EXPORTED" not in os.environ


def test_hello_and_backend_event_shape_are_provider_neutral() -> None:
    provider = ClaudeAgentSdkProvider(
        cwd=".",
        runtime_profile_id="deepseek",
        runtime_profile_name="DeepSeek",
        env_overrides={
            "ANTHROPIC_BASE_URL": "https://example.test",
            "ANTHROPIC_AUTH_TOKEN": "secret",
            "ANTHROPIC_MODEL": "deepseek-v4-pro",
        },
        env_file="/tmp/profile.env",
    )
    hello = provider_hello(provider, connector_version="test")
    assert hello["type"] == "connector.hello"
    assert hello["provider"] == "claude_code"
    assert "app_server.turn_start" in hello["capabilities"]
    assert hello["active_runtime_profile_id"] == "deepseek"
    assert hello["runtime_profiles"][0]["display_name"] == "DeepSeek"
    assert hello["runtime_profiles"][0]["auth_source"] == "env_file"
    assert hello["runtime_profiles"][0]["model"] == "deepseek-v4-pro"
    assert hello["runtime_profiles"][0]["model_source"] == "env_file"
    assert "ANTHROPIC_API_KEY" in hello["runtime_profiles"][0]["env_keys"]
    assert "ANTHROPIC_AUTH_TOKEN" in hello["runtime_profiles"][0]["env_keys"]
    assert hello["runtime_profiles"][0]["has_claude_auth_env"] is True
    assert "secret" not in json.dumps(hello)

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


def test_claude_runtime_profile_marks_cli_default_model(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL", raising=False)
    provider = ClaudeAgentSdkProvider(cwd=".", runtime_profile_id="default")
    profile = provider.runtime_profile_report()
    assert profile["model"] is None
    assert profile["model_source"] == "cli_default"
    assert profile["display_name"] == "CLI default"


def test_claude_approval_envelope_opens_submit_ready_request() -> None:
    provider = ClaudeAgentSdkProvider(cwd=".")
    envelope = provider._envelope(
        "approval.requested",
        {
            "thread_id": "thread_1",
            "turn_id": "turn_1",
            "provider_thread_id": "session_1",
            "provider_turn_id": "turn_provider_1",
        },
        {
            "request_id": "claude-perm-1",
            "provider_request_id": "claude-perm-1",
            "request_fingerprint": "sha256:abc",
            "kind": "command",
            "tool_name": "Bash",
            "command": "df -h /",
            "approval_method": "item/commandExecution/requestApproval",
        },
    )

    opened = approval_envelope_to_request_opened(
        envelope,
        {
            "thread_id": "thread_1",
            "turn_id": "turn_1",
            "provider_thread_id": "session_1",
            "provider_turn_id": "turn_provider_1",
        },
    )

    assert opened is not None
    assert opened["type"] == "app_server.request_opened"
    assert opened["kind"] == "approval"
    assert opened["method"] == "item/commandExecution/requestApproval"
    assert opened["app_server_request_id"] == "claude-perm-1"
    assert opened["app_server_thread_id"] == "session_1"
    assert opened["app_server_turn_id"] == "turn_provider_1"
    assert opened["payload"]["command_preview"] == "df -h /"
    assert opened["payload"]["request_fingerprint"] == "sha256:abc"
    assert opened["payload"]["available_decisions"] == [
        "accept",
        "decline",
        "cancel",
    ]


def test_claude_approval_accept_decisions_are_allowed() -> None:
    assert _approval_decision_allows({"decision": "accept"}) is True
    assert _approval_decision_allows({"decision": "acceptForSession"}) is True
    assert _approval_decision_allows({"app_server_decision": {"accept": {}}}) is True
    assert _approval_decision_allows({"response": {"decision": "accept"}}) is True
    assert _approval_decision_allows({"decision": "decline"}) is False
    assert _approval_decision_allows({"response": {"decision": "cancel"}}) is False


def test_reconnect_command_does_not_include_provider() -> None:
    class Args:
        server = "https://www.botsdock.cn"
        cwd = "."
        model = None

    command = reconnect_command(Args())

    assert command == "botsdock-connector"
    assert "--provider" not in command


def test_parser_defaults_to_claude_cli_on_path() -> None:
    import shutil

    previous = {
        "BOTSDOCK_CLAUDE_BIN": os.environ.get("BOTSDOCK_CLAUDE_BIN"),
        "CLAUDE_CODE_BIN": os.environ.get("CLAUDE_CODE_BIN"),
    }
    original_which = shutil.which
    for key in previous:
        os.environ.pop(key, None)
    shutil.which = lambda command: "/tmp/claude" if command == "claude" else None  # type: ignore[assignment]
    try:
        args = build_parser().parse_args([])
    finally:
        shutil.which = original_which  # type: ignore[assignment]
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert args.claude_bin == "/tmp/claude"


def test_upgrade_subcommand_defaults_to_github_install() -> None:
    args = build_parser().parse_args(["upgrade", "--dry-run"])

    command = build_upgrade_pip_args(args)

    assert args.command == "upgrade"
    assert command == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "git+https://github.com/bourne015/botsdock-connector.git",
    ]


def test_upgrade_subcommand_supports_github_ref_and_pypi_version() -> None:
    github_args = build_parser().parse_args(["upgrade", "--version", "v0.1.1"])
    pypi_args = build_parser().parse_args(
        ["upgrade", "--source", "pypi", "--version", "0.1.1"]
    )

    assert build_upgrade_pip_args(github_args)[-1] == (
        "git+https://github.com/bourne015/botsdock-connector.git@v0.1.1"
    )
    assert build_upgrade_pip_args(pypi_args)[-1] == "botsdock-connector==0.1.1"


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
            runtime_profile={
                "id": "deepseek",
                "display_name": "DeepSeek",
                "env_file": "/tmp/deepseek.env",
                "model": "deepseek-chat",
                "claude_bin": "/usr/local/bin/claude",
            },
            cwd=tmp,
        )

        connectors = load_saved_connectors(server_url=DEFAULT_SERVER, cwd=tmp)

    assert sorted((item.machine_id, item.token, item.provider) for item in connectors) == [
        ("mach_claude", "token_claude", "claude_code"),
        ("mach_codex", "token_codex", "codex"),
    ]
    claude = next(item for item in connectors if item.machine_id == "mach_claude")
    assert claude.runtime_profile_id == "deepseek"
    assert claude.runtime_profile_name == "DeepSeek"
    assert claude.env_file == "/tmp/deepseek.env"
    assert claude.model == "deepseek-chat"
    assert claude.claude_bin == "/usr/local/bin/claude"


def test_token_store_uses_new_filename_and_reads_legacy_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        token_path = save_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_new",
            token="token_new",
            provider="codex",
            cwd=tmp,
        )
        assert token_path.name == ".botsdock_connector.json"

    with tempfile.TemporaryDirectory() as tmp:
        legacy_path = Path(tmp) / ".botsdock_agent_connector.json"
        legacy_key = token_store_key(DEFAULT_SERVER, "mach_legacy")
        legacy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "connectors": {
                        legacy_key: {
                            "server": DEFAULT_SERVER,
                            "machine_id": "mach_legacy",
                            "provider": "claude_code",
                            "token": "token_legacy",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = load_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_legacy",
            cwd=tmp,
        )

    assert loaded == "token_legacy"


def test_no_arg_connection_resolution_supervises_all_saved_connectors() -> None:
    class Args:
        server = DEFAULT_SERVER
        machine_id = None
        token = None
        cwd = ""
        runtime_profile = None
        runtime_profile_name = None
        env_file = None
        model = None
        claude_bin = None

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
            runtime_profile={"id": "deepseek", "env_file": "/tmp/deepseek.env"},
            cwd=tmp,
        )

        specs = resolve_connection_specs(Args())

    assert sorted((item.machine_id, item.provider) for item in specs) == [
        ("mach_claude", "claude_code"),
        ("mach_codex", "codex"),
    ]
    claude = next(item for item in specs if item.machine_id == "mach_claude")
    assert claude.runtime_profile_id == "deepseek"
    assert claude.env_file == "/tmp/deepseek.env"


def test_machine_id_connection_resolution_preserves_saved_runtime_profile() -> None:
    class Args:
        server = DEFAULT_SERVER
        machine_id = "mach_claude"
        token = None
        cwd = ""
        runtime_profile = None
        runtime_profile_name = None
        env_file = None
        model = None
        claude_bin = None

    with tempfile.TemporaryDirectory() as tmp:
        Args.cwd = tmp
        save_connector_token(
            server_url=DEFAULT_SERVER,
            machine_id="mach_claude",
            token="token_claude",
            provider="claude_code",
            runtime_profile={
                "id": "deepseek",
                "display_name": "DeepSeek",
                "env_file": "/tmp/deepseek.env",
                "model": "deepseek-chat",
            },
            cwd=tmp,
        )

        specs = resolve_connection_specs(Args())

    assert len(specs) == 1
    assert specs[0].token == "token_claude"
    assert specs[0].provider == "claude_code"
    assert specs[0].runtime_profile_id == "deepseek"
    assert specs[0].runtime_profile_name == "DeepSeek"
    assert specs[0].env_file == "/tmp/deepseek.env"
    assert specs[0].model == "deepseek-chat"


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


def test_claude_thread_sync_and_history_read_local_transcript() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session_1.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "type": "user",
                        "uuid": "local_caveat_1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sessionId": "session_1",
                        "cwd": tmp,
                        "message": {
                            "role": "user",
                            "content": "<local-command-caveat>Caveat: ignore local commands</local-command-caveat>",
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "user_1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sessionId": "session_1",
                        "cwd": tmp,
                        "message": {"role": "user", "content": "hello"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "assistant_1",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "sessionId": "session_1",
                        "cwd": tmp,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "hi"},
                                {
                                    "type": "tool_use",
                                    "id": "tool_1",
                                    "name": "Bash",
                                    "input": {"command": "pwd"},
                                },
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "tool_result_1",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "sessionId": "session_1",
                        "cwd": tmp,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_1",
                                    "content": "workspace",
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "local_stdout_1",
                        "timestamp": "2026-01-01T00:00:03Z",
                        "sessionId": "session_1",
                        "cwd": tmp,
                        "message": {
                            "role": "user",
                            "content": "<local-command-stdout>Goodbye!</local-command-stdout>",
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )
        other_project = Path(tmp) / "other_project"
        other_project.mkdir()
        (other_project / "session_2.jsonl").write_text(
            "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "type": "user",
                        "uuid": "user_2",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sessionId": "session_2",
                        "cwd": "/tmp/other-project",
                        "message": {"role": "user", "content": "second project"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "assistant_2",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "sessionId": "session_2",
                        "cwd": "/tmp/other-project",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "second reply"}],
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )
        local_only = Path(tmp) / "session_local.jsonl"
        local_only.write_text(
            "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "type": "user",
                        "uuid": "local_only_1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sessionId": "session_local",
                        "message": {
                            "role": "user",
                            "content": "<local-command-caveat>Caveat</local-command-caveat>",
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "local_only_2",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "sessionId": "session_local",
                        "message": {
                            "role": "user",
                            "content": "<command-name>/exit</command-name>",
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )
        subagent_dir = Path(tmp) / "session_1" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-noise.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "subagent_noise_1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "sessionId": "subagent_noise",
                    "message": {"role": "user", "content": "subagent noise"},
                }
            ),
            encoding="utf-8",
        )
        assistant_only = Path(tmp) / "assistant_only.jsonl"
        assistant_only.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "assistant_only_1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "sessionId": "assistant_only",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "No response requested."}],
                    },
                }
            ),
            encoding="utf-8",
        )
        previous = os.environ.get("BOTSDOCK_CLAUDE_TRANSCRIPT_DIR")
        os.environ["BOTSDOCK_CLAUDE_TRANSCRIPT_DIR"] = os.pathsep.join(
            [tmp, str(other_project)]
        )
        try:
            async def run() -> tuple[dict, dict]:
                provider = ClaudeAgentSdkProvider(cwd=tmp)
                provider._list_threads_from_sdk = lambda *, limit: []  # type: ignore[method-assign]
                provider._load_messages_from_sdk = lambda session_id: []  # type: ignore[method-assign]
                connector = ClaudeCodeConnector(provider=provider, outbound=asyncio.Queue())
                sync_response = await connector.handle_backend_message(
                    {"type": "connector.sync_snapshot", "request_id": "req_sync"}
                )
                history_response = await connector.handle_backend_message(
                    {
                        "type": "connector.thread_history",
                        "request_id": "req_1",
                        "payload": {
                            "thread_id": "thread_1",
                            "provider_session_id": "session_1",
                            "direction": "latest",
                        },
                    }
                )
                assert sync_response is not None
                assert history_response is not None
                return sync_response, history_response

            sync_response, history_response = asyncio.run(run())
        finally:
            if previous is None:
                os.environ.pop("BOTSDOCK_CLAUDE_TRANSCRIPT_DIR", None)
            else:
                os.environ["BOTSDOCK_CLAUDE_TRANSCRIPT_DIR"] = previous

    assert sync_response["status"] == "ok"
    synced_sessions = {
        item["provider_session_id"] for item in sync_response["payload"]["threads"]
    }
    assert "session_1" in synced_sessions
    assert "session_2" in synced_sessions
    assert "session_local" not in synced_sessions
    assert "assistant_only" not in synced_sessions
    session_2 = next(
        item
        for item in sync_response["payload"]["threads"]
        if item["provider_session_id"] == "session_2"
    )
    assert session_2["remote_path"] == str(Path("/tmp/other-project").resolve())
    assert history_response["status"] == "ok"
    assert history_response["payload"]["type"] == "thread.history"
    assert len(history_response["payload"]["turns"]) == 1
    turn = history_response["payload"]["turns"][0]
    assert [item["type"] for item in turn["items"]] == [
        "userMessage",
        "agentMessage",
        "commandExecution",
    ]
    assert turn["items"][1]["text"] == "hi"
    assert turn["items"][2]["command"] == "pwd"
    assert turn["items"][2]["aggregatedOutput"] == "workspace"


def test_claude_options_use_user_cli_settings_and_env() -> None:
    class FakeSdk:
        @staticmethod
        def ClaudeAgentOptions(**kwargs):
            return kwargs

    provider = ClaudeAgentSdkProvider(
        cwd=".",
        default_cwd=".",
        cli_path="/usr/local/bin/claude",
    )
    provider._sdk = FakeSdk()
    previous = os.environ.get("ANTHROPIC_BASE_URL")
    os.environ["ANTHROPIC_BASE_URL"] = "https://example.test/anthropic"
    try:
        options = provider._build_options(
            _request(),
            cwd=Path(".").resolve(),
            can_use_tool=None,
        )
    finally:
        if previous is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = previous

    assert options["cli_path"] == "/usr/local/bin/claude"
    assert options["setting_sources"] == ["user", "project", "local"]
    assert options["env"]["ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"
    assert options["env"]["CLAUDE_AGENT_SDK_CLIENT_APP"] == "botsdock-connector"


def test_claude_child_env_preserves_cli_login_lookup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.test/anthropic")

    provider = ClaudeAgentSdkProvider(cwd=".")

    env = provider._claude_env_overrides()
    report = provider.runtime_profile_report()

    assert env["HOME"] == str(tmp_path)
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / ".config")
    assert env["ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"
    assert "HOME" not in report["env_keys"]
    assert "PATH" not in report["env_keys"]
    assert "XDG_CONFIG_HOME" not in report["env_keys"]
    assert "ANTHROPIC_BASE_URL" in report["env_keys"]


def test_claude_env_auth_token_is_mirrored_for_sdk_resume() -> None:
    provider = ClaudeAgentSdkProvider(
        cwd=".",
        env_overrides={
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "token-from-env-file",
            "OPENAI_API_KEY": "openai-token",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
        },
    )

    env = provider._claude_env_overrides()
    report = provider.runtime_profile_report()

    assert env["ANTHROPIC_API_KEY"] == "token-from-env-file"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "token-from-env-file"
    assert "OPENAI_API_KEY" in report["env_keys"]
    assert "HTTPS_PROXY" in report["env_keys"]
    assert "token-from-env-file" not in json.dumps(report)


def test_openai_key_alone_is_not_reported_as_claude_auth() -> None:
    auth_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    previous = {key: os.environ.get(key) for key in auth_keys}
    for key in auth_keys:
        os.environ.pop(key, None)
    try:
        provider = ClaudeAgentSdkProvider(
            cwd=".",
            env_overrides={
                "OPENAI_API_KEY": "openai-token",
                "HTTPS_PROXY": "http://127.0.0.1:7890",
            },
        )
        report = provider.runtime_profile_report()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert report["auth_source"] == "claude_cli_settings"
    assert report["has_claude_auth_env"] is False
    assert report["gateway_env_keys"] == ["OPENAI_API_KEY"]
    assert "openai-token" not in json.dumps(report)


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
