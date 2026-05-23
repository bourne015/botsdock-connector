from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shlex
import shutil
import signal
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .daemon import (
    daemon_restart,
    daemon_start,
    daemon_status,
    daemon_stop,
    install_signal_handlers,
)
from .log import get_logger
from .protocol import CONNECTION_MODE, PROTOCOL_VERSION
from .providers.claude_agent_sdk import ClaudeAgentSdkProvider, _runtime_profile_log_line, _should_warn_missing_claude_env
from .register import run_register
from .providers.codex_app_server import (
    CodexConnector,
    _version_label,
)
from .providers.app_server_client import AppServerProcessClient
from .providers.delta_buffer import BufferedBackendSender
from .session import (
    heartbeat_sender,
    outbound_writer,
    message_loop,
    send_bootstrap,
    save_accepted_token as save_token,
)
from .token_store import (
    DEFAULT_SERVER,
    ConnectorError,
    SavedConnector,
    backend_ws_url,
    load_connector_token,
    load_saved_connectors,
)
from .upgrade import run_upgrade

logger = get_logger(__name__)

JsonDict = dict[str, Any]
CODEX_AGENT_PROVIDER = "codex"
CLAUDE_CODE_AGENT_PROVIDER = "claude_code"
CONNECTOR_MACHINE_PROVIDER = "agent"
SUPPORTED_CONNECTOR_PROVIDERS = {
    CONNECTOR_MACHINE_PROVIDER,
    CODEX_AGENT_PROVIDER,
    CLAUDE_CODE_AGENT_PROVIDER,
}
DEFAULT_RUNTIME_PROFILE_ID = "default"


@dataclass
class ConnectionSpec:
    server: str
    machine_id: str
    token: str
    cwd: str
    provider: str | None = None
    default_workspace_cwd: str | None = None
    runtime_profile_id: str = DEFAULT_RUNTIME_PROFILE_ID
    runtime_profile_name: str | None = None
    env_file: str | None = None
    model: str | None = None
    claude_bin: str | None = None


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="botsdock-connector",
        description="Run the BotsDock connector",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--server",
        default=os.environ.get(
            "BOTSDOCK_CONNECTOR_SERVER",
            os.environ.get(
                "BOTSDOCK_AGENT_SERVER",
                os.environ.get("BOTSDOCK_CODEX_SERVER", DEFAULT_SERVER),
            ),
        ),
        help=(
            "Backend base URL. Defaults to BOTSDOCK_CONNECTOR_SERVER, "
            f"BOTSDOCK_CODEX_SERVER, or {DEFAULT_SERVER}."
        ),
    )
    parser.add_argument("--machine-id", default=None)
    parser.add_argument(
        "--token",
        default=None,
        help="Registration token for first connection. Omit after the connector token has been saved.",
    )
    parser.add_argument("--cwd", default=None, help="optional default workspace root")
    parser.add_argument(
        "--runtime-profile",
        default=os.environ.get("BOTSDOCK_CONNECTOR_PROFILE")
        or os.environ.get("BOTSDOCK_AGENT_PROFILE")
        or os.environ.get("BOTSDOCK_CLAUDE_PROFILE"),
        help="Local runtime profile id for provider-specific CLI/env settings. Defaults to 'default'.",
    )
    parser.add_argument(
        "--runtime-profile-name",
        default=os.environ.get("BOTSDOCK_CONNECTOR_PROFILE_NAME")
        or os.environ.get("BOTSDOCK_AGENT_PROFILE_NAME")
        or os.environ.get("BOTSDOCK_CLAUDE_PROFILE_NAME"),
        help="Optional display name for the local runtime profile.",
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("BOTSDOCK_CONNECTOR_ENV_FILE")
        or os.environ.get("BOTSDOCK_AGENT_ENV_FILE"),
        help="Local env file for provider credentials.",
    )
    parser.add_argument("--model", default=None, help="provider model override")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--claude-bin",
        default=os.environ.get("BOTSDOCK_CLAUDE_BIN")
        or os.environ.get("CLAUDE_CODE_BIN")
        or shutil.which("claude"),
        help="Claude Code CLI path.",
    )
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--open-timeout", type=float, default=60)
    parser.add_argument("--ping-timeout", type=float, default=60)
    parser.add_argument("--approval-timeout", type=float, default=900)
    parser.add_argument("--connector-version", default=__version__)
    parser.add_argument("--delta-flush-interval", type=float, default=0.12)
    parser.add_argument("--delta-flush-chars", type=int, default=768)
    parser.add_argument(
        "--no-reconnect",
        dest="reconnect",
        action="store_false",
        help="Exit instead of reconnecting after the backend WebSocket closes",
    )
    parser.add_argument("--reconnect-initial-delay", type=float, default=1.0)
    parser.add_argument("--reconnect-max-delay", type=float, default=30.0)
    subparsers = parser.add_subparsers(dest="command")
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Upgrade botsdock-connector in the current Python environment",
    )
    upgrade_parser.add_argument(
        "--source",
        choices=("github", "pypi"),
        default="github",
        help="Upgrade source. Defaults to github.",
    )
    upgrade_parser.add_argument(
        "--version",
        default=None,
        help="Optional version or git ref.",
    )
    upgrade_parser.add_argument(
        "--package-spec",
        default=os.environ.get("BOTSDOCK_CONNECTOR_UPGRADE_SPEC"),
        help="Override the pip package spec.",
    )
    upgrade_parser.add_argument(
        "--user",
        action="store_true",
        help="Pass --user to pip when upgrading outside a virtual environment.",
    )
    upgrade_parser.add_argument(
        "--pre",
        action="store_true",
        help="Allow pre-release versions when upgrading from PyPI.",
    )
    upgrade_parser.add_argument(
        "--force-reinstall",
        action="store_true",
        help="Ask pip to reinstall even if the selected version is already installed.",
    )
    upgrade_parser.add_argument(
        "--pip-arg",
        action="append",
        default=[],
        help="Additional argument passed through to pip. Repeat for multiple args.",
    )
    upgrade_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pip command without running it.",
    )
    # Daemon lifecycle subcommands.
    start_parser = subparsers.add_parser(
        "start",
        help="Start the connector as a background daemon",
    )
    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop a running daemon",
    )
    restart_parser = subparsers.add_parser(
        "restart",
        help="Restart the daemon (stop + start)",
    )
    status_parser = subparsers.add_parser(
        "status",
        help="Print daemon status",
    )
    register_parser = subparsers.add_parser(
        "register",
        help="Register this machine with a short code",
    )
    register_parser.add_argument(
        "--renew",
        action="store_true",
        help="Re-register an existing machine (refreshes token)",
    )
    parser.set_defaults(reconnect=True)
    return parser


# ---------------------------------------------------------------------------
# Runtime profile helpers
# ---------------------------------------------------------------------------

def normalize_runtime_profile_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_RUNTIME_PROFILE_ID


def default_env_file(runtime_profile_id: str | None = None) -> str | None:
    configured = os.environ.get("BOTSDOCK_CONNECTOR_ENV_FILE") or os.environ.get(
        "BOTSDOCK_AGENT_ENV_FILE"
    )
    if configured:
        return configured
    profile_id = normalize_runtime_profile_id(runtime_profile_id)
    if profile_id != DEFAULT_RUNTIME_PROFILE_ID:
        profile_path = Path.home() / ".botsdock" / f"botsdock_connector.{profile_id}.env"
        if profile_path.exists():
            return str(profile_path)
    default_path = Path.home() / ".botsdock" / "botsdock_connector.env"
    if default_path.exists():
        return str(default_path)
    return None


def load_env_file(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path).expanduser()
    if not env_path.exists():
        raise ConnectorError(f"env file not found: {env_path}")
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
    return loaded


# ---------------------------------------------------------------------------
# Connection spec resolution
# ---------------------------------------------------------------------------

def connector_cwd(args: argparse.Namespace) -> str:
    return str(Path(args.cwd or ".").expanduser().resolve())


def _connection_args(args: argparse.Namespace, spec: ConnectionSpec) -> argparse.Namespace:
    connection_args = argparse.Namespace(**vars(args))
    registration_only = bool(getattr(args, "token", None))
    connection_args.server = spec.server
    connection_args.machine_id = spec.machine_id
    connection_args.token = spec.token
    connection_args.cwd = spec.cwd
    connection_args.default_workspace_cwd = spec.default_workspace_cwd
    connection_args.runtime_profile = spec.runtime_profile_id
    connection_args.runtime_profile_id = spec.runtime_profile_id
    connection_args.runtime_profile_name = spec.runtime_profile_name
    connection_args.env_file = spec.env_file
    connection_args.model = spec.model
    connection_args.claude_bin = spec.claude_bin
    connection_args.connection_spec = spec
    connection_args.registration_only = registration_only
    return connection_args


def _effective_runtime_profile_id(
    args: argparse.Namespace,
    saved: str | None = None,
) -> str:
    return normalize_runtime_profile_id(
        getattr(args, "runtime_profile", None)
        or getattr(args, "runtime_profile_id", None)
        or saved
    )


def _effective_runtime_profile_name(
    args: argparse.Namespace,
    saved: str | None = None,
) -> str | None:
    value = getattr(args, "runtime_profile_name", None) or saved
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _effective_env_file(
    args: argparse.Namespace,
    *,
    runtime_profile_id: str,
    saved: str | None = None,
) -> str | None:
    value = getattr(args, "env_file", None) or saved
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default_env_file(runtime_profile_id)


def _spec_from_saved(
    connector: SavedConnector,
    *,
    args: argparse.Namespace,
    cwd: str,
    default_workspace_cwd: str | None,
) -> ConnectionSpec:
    runtime_profile_id = _effective_runtime_profile_id(
        args,
        saved=connector.runtime_profile_id,
    )
    return ConnectionSpec(
        server=connector.server,
        machine_id=connector.machine_id,
        token=connector.token,
        cwd=cwd,
        provider=connector.provider,
        default_workspace_cwd=default_workspace_cwd,
        runtime_profile_id=runtime_profile_id,
        runtime_profile_name=_effective_runtime_profile_name(
            args,
            saved=connector.runtime_profile_name,
        ),
        env_file=_effective_env_file(
            args,
            runtime_profile_id=runtime_profile_id,
            saved=connector.env_file,
        ),
        model=getattr(args, "model", None) or connector.model,
        claude_bin=getattr(args, "claude_bin", None) or connector.claude_bin,
    )


def resolve_connection_specs(args: argparse.Namespace) -> list[ConnectionSpec]:
    cwd = connector_cwd(args)
    default_workspace_cwd = cwd if args.cwd else None
    machine_id = args.machine_id
    token = args.token
    if token and not machine_id:
        raise ConnectorError("missing machine-id for registration token connection")
    if machine_id:
        saved_connector = None
        if not token:
            saved_connector = next(
                (
                    connector
                    for connector in load_saved_connectors(server_url=args.server, cwd=cwd)
                    if connector.machine_id == machine_id
                ),
                None,
            )
            if saved_connector is not None:
                token = saved_connector.token
        if not token:
            token = load_connector_token(
                server_url=args.server,
                machine_id=machine_id,
                cwd=cwd,
            )
        if not token:
            raise ConnectorError("missing connector token. Run the web-generated registration command once.")
        runtime_profile_id = _effective_runtime_profile_id(
            args,
            saved=saved_connector.runtime_profile_id if saved_connector is not None else None,
        )
        return [
            ConnectionSpec(
                server=args.server.rstrip("/"),
                machine_id=machine_id,
                token=token,
                cwd=cwd,
                provider=saved_connector.provider if saved_connector is not None else None,
                default_workspace_cwd=default_workspace_cwd,
                runtime_profile_id=runtime_profile_id,
                runtime_profile_name=_effective_runtime_profile_name(
                    args,
                    saved=saved_connector.runtime_profile_name if saved_connector is not None else None,
                ),
                env_file=_effective_env_file(
                    args,
                    runtime_profile_id=runtime_profile_id,
                    saved=saved_connector.env_file if saved_connector is not None else None,
                ),
                model=getattr(args, "model", None)
                or (saved_connector.model if saved_connector is not None else None),
                claude_bin=getattr(args, "claude_bin", None)
                or (saved_connector.claude_bin if saved_connector is not None else None),
            )
        ]

    saved_connectors = load_saved_connectors(server_url=args.server, cwd=cwd)
    if not saved_connectors:
        raise ConnectorError("missing connector token. Run the web-generated registration command once.")
    return [
        _spec_from_saved(
            connector,
            args=args,
            cwd=cwd,
            default_workspace_cwd=default_workspace_cwd,
        )
        for connector in saved_connectors
    ]


def connection_label(spec: ConnectionSpec) -> str:
    provider = f" provider={spec.provider}" if spec.provider else ""
    profile = (
        f" profile={spec.runtime_profile_id}"
        if spec.runtime_profile_id != DEFAULT_RUNTIME_PROFILE_ID
        else ""
    )
    return f"machine={spec.machine_id}{provider}{profile}"


# ---------------------------------------------------------------------------
# Protocol message helpers
# ---------------------------------------------------------------------------

def provider_hello(provider: ClaudeAgentSdkProvider, *, connector_version: str) -> JsonDict:
    hello: JsonDict = {
        "type": "connector.hello",
        "provider": provider.name,
        "connector_version": connector_version,
        "platform": sys.platform,
        "hostname": socket.gethostname(),
        "protocol_version": PROTOCOL_VERSION,
        "connection_mode": CONNECTION_MODE,
        "capabilities": [
            "app_server.thread_start",
            "app_server.thread_resume",
            "app_server.thread_archive",
            "app_server.thread_unarchive",
            "app_server.thread_delete",
            "app_server.turn_start",
            "app_server.turn_cancel",
            "app_server.approval_respond",
            "connector.sync_snapshot",
            "connector.thread_history",
            "workspace.report",
            "thread.sync",
        ],
        "provider_runtime": {
            "name": provider.name,
            "active_runtime_profile_id": provider.active_runtime_profile_id,
            "capabilities": {
                "can_resume_session": provider.capabilities.can_resume_session,
                "can_cancel_turn": provider.capabilities.can_cancel_turn,
                "can_request_approval": provider.capabilities.can_request_approval,
                "can_report_file_activity": provider.capabilities.can_report_file_activity,
                "event_types": list(provider.capabilities.event_types),
            },
        },
    }
    runtime_profiles = provider.runtime_profiles()
    if runtime_profiles:
        hello["runtime_profiles"] = runtime_profiles
        hello["active_runtime_profile_id"] = provider.active_runtime_profile_id
    return hello


def _capabilities_from_hello(hello: JsonDict) -> list[str]:
    raw = hello.get("capabilities")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item is not None]


def _provider_runtime_from_hello(
    hello: JsonDict,
    *,
    provider: str,
    display_name: str,
    runtime: str,
    version: str | None = None,
) -> JsonDict:
    runtime_profiles = hello.get("runtime_profiles")
    if not isinstance(runtime_profiles, list):
        runtime_profiles = []
    return {
        "provider": provider,
        "display_name": display_name,
        "runtime": runtime,
        "version": version,
        "active_runtime_profile_id": hello.get("active_runtime_profile_id"),
        "capabilities": _capabilities_from_hello(hello),
        "runtime_profiles": runtime_profiles,
    }


def _agent_hello(provider_runtimes: list[JsonDict], *, connector_version: str) -> JsonDict:
    capabilities = sorted(
        {
            "provider.runtime_mux",
            *(
                str(capability)
                for runtime in provider_runtimes
                for capability in runtime.get("capabilities", [])
            ),
        }
    )
    runtime_profiles = [
        profile
        for runtime in provider_runtimes
        for profile in runtime.get("runtime_profiles", [])
        if isinstance(profile, dict)
    ]
    hello: JsonDict = {
        "type": "connector.hello",
        "provider": CONNECTOR_MACHINE_PROVIDER,
        "connector_version": connector_version,
        "platform": sys.platform,
        "hostname": socket.gethostname(),
        "protocol_version": PROTOCOL_VERSION,
        "connection_mode": CONNECTION_MODE,
        "capabilities": capabilities,
        "provider_runtimes": provider_runtimes,
    }
    if runtime_profiles:
        hello["runtime_profiles"] = runtime_profiles
    for runtime in provider_runtimes:
        if runtime.get("provider") == CODEX_AGENT_PROVIDER and runtime.get("app_server"):
            hello["app_server"] = runtime["app_server"]
            break
    return hello


def ok_response(request_id: Any, payload: JsonDict | None = None) -> JsonDict:
    return {
        "type": "connector.response",
        "request_id": request_id,
        "status": "ok",
        "payload": payload or {},
    }


def error_response(request_id: Any, code: str, message: str) -> JsonDict:
    return {
        "type": "connector.response",
        "request_id": request_id,
        "status": "error",
        "error": {"code": code, "message": message},
    }


def envelope_to_backend_message(
    envelope,
    request: JsonDict,
    *,
    request_id: str | None = None,
) -> JsonDict:
    payload = dict(envelope.payload or {})
    provider = payload.get("provider") or request.get("provider")
    thread_id = payload.get("thread_id") or request.get("thread_id")
    turn_id = payload.get("turn_id") or request.get("turn_id")
    return {
        "type": "app_server.event",
        "provider": provider,
        "event_type": envelope.type,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "request_id": request_id,
        "provider_event_id": envelope.provider_event_id,
        "provider_thread_id": envelope.provider_thread_id,
        "provider_turn_id": envelope.provider_turn_id,
        "provider_session_id": envelope.provider_session_id,
        "payload": {
            **payload,
            "provider": provider,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "provider_event_id": envelope.provider_event_id,
            "provider_thread_id": envelope.provider_thread_id,
            "provider_turn_id": envelope.provider_turn_id,
            "provider_session_id": envelope.provider_session_id,
        },
        "raw_provider_event": envelope.raw_event,
    }


def approval_envelope_to_request_opened(envelope, request: JsonDict) -> JsonDict | None:
    payload = dict(envelope.payload or {})
    provider = payload.get("provider") or request.get("provider")
    app_server_request_id = (
        payload.get("app_server_request_id")
        or payload.get("request_id")
        or payload.get("provider_request_id")
    )
    if app_server_request_id is None:
        return None
    app_server_request_id = str(app_server_request_id)
    method = (
        payload.get("approval_method")
        or payload.get("method")
        or "item/commandExecution/requestApproval"
    )
    app_server_thread_id = (
        payload.get("app_server_thread_id")
        or payload.get("appServerThreadId")
        or request.get("app_server_thread_id")
        or request.get("provider_thread_id")
        or request.get("thread_id")
    )
    app_server_turn_id = (
        payload.get("app_server_turn_id")
        or payload.get("appServerTurnId")
        or request.get("app_server_turn_id")
        or request.get("provider_turn_id")
        or request.get("turn_id")
    )
    if app_server_thread_id is None:
        return None
    command = payload.get("command")
    if isinstance(command, str) and command:
        payload.setdefault("command_preview", command)
    payload.setdefault("request_id", app_server_request_id)
    payload.setdefault("app_server_request_id", app_server_request_id)
    payload.setdefault("app_server_thread_id", str(app_server_thread_id))
    if app_server_turn_id is not None:
        payload.setdefault("app_server_turn_id", str(app_server_turn_id))
    payload.setdefault("approval_method", method)
    payload.setdefault("available_decisions", ["accept", "decline", "cancel"])
    return {
        "type": "app_server.request_opened",
        "provider": provider,
        "kind": "approval",
        "method": method,
        "thread_id": request.get("thread_id") or payload.get("thread_id"),
        "turn_id": request.get("turn_id") or payload.get("turn_id"),
        "app_server_thread_id": str(app_server_thread_id),
        "app_server_turn_id": str(app_server_turn_id)
        if app_server_turn_id is not None
        else None,
        "app_server_request_id": app_server_request_id,
        "request_fingerprint": payload.get("request_fingerprint"),
        "payload": payload,
        "raw_payload": envelope.raw_event,
    }


def workspace_report(cwd: str) -> JsonDict:
    path = str(Path(cwd).expanduser().resolve())
    return {
        "type": "workspace.report",
        "workspaces": [
            {
                "name": Path(path).name or path,
                "path": path,
            }
        ],
    }


def _list_from_message(message: JsonDict, key: str) -> list[Any]:
    value = message.get(key)
    if isinstance(value, list):
        return value
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _message_provider(message: JsonDict) -> str | None:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    value = message.get("provider") or payload.get("provider")
    if isinstance(value, str) and value:
        return value
    for item in [*_list_from_message(message, "threads"), *_list_from_message(message, "workspaces")]:
        if isinstance(item, dict):
            provider = item.get("provider")
            if isinstance(provider, str) and provider:
                return provider
    return None


def _tag_provider_message(message: JsonDict, provider: str) -> JsonDict:
    tagged = dict(message)
    tagged["provider"] = provider
    payload = tagged.get("payload")
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("provider", provider)
        tagged["payload"] = payload
    for key in ("threads", "workspaces"):
        items = tagged.get(key)
        if isinstance(items, list):
            tagged[key] = [
                {**item, "provider": item.get("provider") or provider}
                if isinstance(item, dict)
                else item
                for item in items
            ]
    return tagged


def reconnect_command(args: argparse.Namespace) -> str:
    parts = ["botsdock-connector"]
    if args.server.rstrip("/") != DEFAULT_SERVER:
        parts.extend(["--server", args.server])
    if args.cwd and args.cwd != ".":
        parts.extend(["--cwd", args.cwd])
    runtime_profile_id = normalize_runtime_profile_id(
        getattr(args, "runtime_profile_id", None) or getattr(args, "runtime_profile", None)
    )
    if runtime_profile_id != DEFAULT_RUNTIME_PROFILE_ID:
        parts.extend(["--runtime-profile", runtime_profile_id])
    runtime_profile_name = getattr(args, "runtime_profile_name", None)
    if runtime_profile_name:
        parts.extend(["--runtime-profile-name", runtime_profile_name])
    env_file = getattr(args, "env_file", None)
    if env_file:
        parts.extend(["--env-file", env_file])
    claude_bin = getattr(args, "claude_bin", None)
    if claude_bin:
        parts.extend(["--claude-bin", claude_bin])
    if getattr(args, "model", None):
        parts.extend(["--model", args.model])
    return " ".join(shlex.quote(str(part)) for part in parts)


# ---------------------------------------------------------------------------
# ClaudeCodeConnector
# ---------------------------------------------------------------------------

class ClaudeCodeConnector:
    def __init__(self, *, provider: ClaudeAgentSdkProvider, outbound: asyncio.Queue[JsonDict]) -> None:
        self.provider = provider
        self.outbound = outbound
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}

    async def handle_backend_message(self, message: JsonDict) -> JsonDict | None:
        msg_type = message.get("type")
        request_id = message.get("request_id")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if msg_type == "app_server.turn_start":
            turn_id = str(payload.get("turn_id") or request_id)
            logger.info(
                "claude turn start: thread=%s turn=%s cwd=%s session=%s",
                payload.get("thread_id"), turn_id,
                payload.get("cwd"), payload.get("provider_session_id"),
            )
            task = asyncio.create_task(self._run_turn(payload, request_id=request_id))
            self._turn_tasks[turn_id] = task
            task.add_done_callback(lambda done, key=turn_id: self._turn_tasks.pop(key, None))
            return ok_response(
                request_id,
                {"accepted": True, "provider": self.provider.name, "turn_id": turn_id},
            )
        if msg_type == "app_server.turn_steer":
            return error_response(
                request_id,
                "unsupported_request",
                "Claude Code connector does not support steering an active turn yet",
            )
        if msg_type == "app_server.turn_cancel":
            envelope = await self.provider.cancel_turn(payload)
            await self.outbound.put(
                envelope_to_backend_message(envelope, payload, request_id=request_id)
            )
            return ok_response(request_id, {"cancelled": True})
        if msg_type == "app_server.approval_respond":
            envelope = await self.provider.resolve_approval(payload)
            await self.outbound.put(
                envelope_to_backend_message(envelope, payload, request_id=request_id)
            )
            return ok_response(request_id, {"resolved": True})
        if msg_type == "app_server.thread_resume":
            return ok_response(
                request_id,
                {
                    "resumed": True,
                    "provider": self.provider.name,
                    "provider_session_id": payload.get("provider_session_id"),
                },
            )
        if msg_type == "app_server.thread_archive":
            return ok_response(
                request_id,
                {
                    "archived": True,
                    "provider": self.provider.name,
                    "provider_session_id": payload.get("provider_session_id"),
                    "local_only": True,
                },
            )
        if msg_type == "app_server.thread_unarchive":
            return ok_response(
                request_id,
                {
                    "unarchived": True,
                    "provider": self.provider.name,
                    "provider_session_id": payload.get("provider_session_id"),
                    "local_only": True,
                },
            )
        if msg_type == "app_server.thread_delete":
            try:
                return ok_response(request_id, self.provider.delete_thread(payload))
            except Exception as exc:
                return error_response(
                    request_id,
                    "thread_delete_failed",
                    str(exc) or type(exc).__name__,
                )
        if msg_type == "connector.sync_snapshot":
            return ok_response(request_id, self.provider.thread_sync_report())
        if msg_type == "connector.thread_history":
            return ok_response(request_id, self.provider.read_thread_history(payload))
        if msg_type == "app_server.account_snapshot":
            return ok_response(
                request_id,
                {
                    "provider": self.provider.name,
                    "runtime": "claude_agent_sdk",
                    "cwd": self.provider.cwd,
                    "runtime_profile": self.provider.runtime_profile_report(),
                },
            )
        if msg_type in {
            "thread.sync_ack",
            "workspace.report_ack",
            "connector.event_ack",
            "connector.transient_ack",
            "connector.heartbeat_ack",
            "app_server.request_opened_ack",
        }:
            return None
        if request_id is not None:
            return error_response(
                request_id,
                "unsupported_request",
                f"unsupported backend request type: {msg_type}",
            )
        return None

    async def _run_turn(self, request: JsonDict, *, request_id: str | None) -> None:
        async for envelope in self.provider.start_turn(request):
            if envelope.type == "approval.requested":
                opened = approval_envelope_to_request_opened(envelope, request)
                if opened is not None:
                    logger.info(
                        "claude approval requested: thread=%s turn=%s request=%s method=%s",
                        request.get("thread_id"), request.get("turn_id"),
                        opened.get("app_server_request_id"), opened.get("method"),
                    )
                    await self.outbound.put(opened)
                    continue
            if envelope.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                payload = envelope.payload or {}
                error = payload.get("error")
                if isinstance(error, dict):
                    error_text = error.get("message") or error.get("code")
                else:
                    error_text = error
                logger.info(
                    "claude turn terminal: type=%s thread=%s turn=%s session=%s%s",
                    envelope.type, request.get("thread_id"), request.get("turn_id"),
                    payload.get("provider_session_id"),
                    f" error={str(error_text)[:120]}" if error_text else "",
                )
            await self.outbound.put(
                envelope_to_backend_message(
                    envelope,
                    request,
                    request_id=request_id,
                )
            )

    async def stop(self) -> None:
        for task in list(self._turn_tasks.values()):
            task.cancel()
        for task in list(self._turn_tasks.values()):
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        try:
            await asyncio.wait_for(self.provider.stop(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass


# ---------------------------------------------------------------------------
# AgentConnectorMux
# ---------------------------------------------------------------------------

class AgentConnectorMux:
    def __init__(self, runtimes: dict[str, JsonDict]) -> None:
        self.runtimes = runtimes

    def providers(self) -> list[str]:
        return list(self.runtimes)

    async def handle_backend_message(self, message: JsonDict) -> JsonDict | None:
        msg_type = message.get("type")
        request_id = message.get("request_id")
        provider = _message_provider(message)
        if msg_type == "connector.sync_snapshot" and not provider:
            return ok_response(request_id, await self._sync_snapshot(message))
        runtime = self._select_runtime(provider)
        if runtime is None:
            if request_id is None:
                return None
            return error_response(
                request_id,
                "provider_runtime_missing",
                f"provider runtime is not available: {provider or 'unknown'}",
            )
        tagged = _tag_provider_message(message, runtime["provider"])
        connector = runtime["connector"]
        if runtime["kind"] == CODEX_AGENT_PROVIDER:
            return await asyncio.to_thread(connector.handle_backend_message, tagged)
        return await connector.handle_backend_message(tagged)

    def replay_pending_approvals(self) -> None:
        runtime = self.runtimes.get(CODEX_AGENT_PROVIDER)
        if runtime is None:
            return
        connector = runtime.get("connector")
        if connector is not None:
            connector.replay_pending_approvals()

    async def stop(self) -> None:
        for runtime in self.runtimes.values():
            connector = runtime.get("connector")
            if connector is not None and hasattr(connector, "stop"):
                try:
                    await asyncio.wait_for(connector.stop(), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
        for runtime in self.runtimes.values():
            app_server = runtime.get("app_server")
            if app_server is not None:
                await asyncio.to_thread(app_server.close)

    def _select_runtime(self, provider: str | None) -> JsonDict | None:
        if provider and provider in self.runtimes:
            return self.runtimes[provider]
        if provider == CONNECTOR_MACHINE_PROVIDER:
            return None
        if provider is None and len(self.runtimes) == 1:
            return next(iter(self.runtimes.values()))
        return None

    async def _sync_snapshot(self, message: JsonDict) -> JsonDict:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        try:
            limit = max(1, min(int(payload.get("limit") or 200), 500))
        except (TypeError, ValueError):
            limit = 200
        threads: list[JsonDict] = []
        workspaces: list[JsonDict] = []
        for provider, runtime in self.runtimes.items():
            report = await self._runtime_thread_sync(runtime, limit=limit)
            tagged = _tag_provider_message(report, provider)
            threads.extend(item for item in tagged.get("threads", []) if isinstance(item, dict))
            workspaces.extend(item for item in tagged.get("workspaces", []) if isinstance(item, dict))
        return {
            "type": "thread.sync",
            "threads": threads,
            "workspaces": workspaces,
            "authoritative": False,
        }

    async def _runtime_thread_sync(self, runtime: JsonDict, *, limit: int = 200) -> JsonDict:
        connector = runtime["connector"]
        if runtime["kind"] == CODEX_AGENT_PROVIDER:
            return await asyncio.to_thread(lambda: connector.thread_sync_report(limit=limit))
        provider = runtime["provider_object"]
        return provider.thread_sync_report(limit=limit)


async def _send_initial_runtime_sync(websocket: Any, mux: AgentConnectorMux) -> None:
    for provider, runtime in mux.runtimes.items():
        report = await mux._runtime_thread_sync(runtime, limit=200)
        tagged_report = _tag_provider_message(report, provider)
        workspaces = tagged_report.get("workspaces")
        if isinstance(workspaces, list) and workspaces:
            await websocket.send(
                json.dumps(
                    _tag_provider_message(
                        {"type": "workspace.report", "workspaces": workspaces},
                        provider,
                    ),
                    separators=(",", ":"),
                )
            )
        await websocket.send(json.dumps(tagged_report, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Agent provider session (multi-runtime mux)
# ---------------------------------------------------------------------------

def _log_runtime_warning(args: argparse.Namespace, msg: str, *fmt_args: Any) -> None:
    """Log a runtime warning. During registration-only, use info level."""
    if getattr(args, "registration_only", False):
        logger.info(msg, *fmt_args)
    else:
        logger.warning(msg, *fmt_args)


async def run_agent_provider_session(
    *,
    websocket: Any,
    args: argparse.Namespace,
    connector_cwd: str,
    machine_id: str,
) -> None:
    loop = asyncio.get_running_loop()
    outbound: asyncio.Queue[JsonDict] = asyncio.Queue(maxsize=1000)
    runtimes: dict[str, JsonDict] = {}
    provider_runtimes: list[JsonDict] = []

    buffered_sender = BufferedBackendSender(
        loop=loop,
        outbound=outbound,
        flush_interval=args.delta_flush_interval,
        max_chars=args.delta_flush_chars,
    )

    codex_app_server: AppServerProcessClient | None = None
    codex_connector: CodexConnector | None = None
    try:
        codex_bin = getattr(args, "codex_bin", "codex") or "codex"
        if not Path(str(codex_bin)).is_absolute() and shutil.which(str(codex_bin)) is None:
            raise ConnectorError(f"codex binary not found: {codex_bin}")

        def on_appserver_message(message: JsonDict) -> None:
            if codex_connector is not None:
                codex_connector.handle_appserver_message(message)

        codex_app_server = AppServerProcessClient(
            codex_bin=codex_bin,
            cwd=connector_cwd,
            timeout=args.timeout,
            on_message=on_appserver_message,
        )
        codex_connector = CodexConnector(
            app_server=codex_app_server,
            cwd=connector_cwd,
            model=args.model,
        )

        def send_codex(message: JsonDict) -> Any:
            return buffered_sender.send(_tag_provider_message(message, CODEX_AGENT_PROVIDER))

        codex_connector.bind_backend_sender(send_codex)
        init_result = codex_connector.initialize_app_server()
        codex_hello = codex_connector.hello(connector_version=args.connector_version)
        codex_hello["connector_version"] = args.connector_version
        codex_hello["app_server"]["version"] = _version_label(init_result.get("userAgent"))
        codex_runtime = _provider_runtime_from_hello(
            codex_hello,
            provider=CODEX_AGENT_PROVIDER,
            display_name="Codex",
            runtime="codex_app_server",
            version=codex_hello.get("app_server", {}).get("version"),
        )
        codex_runtime["app_server"] = codex_hello.get("app_server")
        provider_runtimes.append(codex_runtime)
        runtimes[CODEX_AGENT_PROVIDER] = {
            "provider": CODEX_AGENT_PROVIDER,
            "kind": CODEX_AGENT_PROVIDER,
            "connector": codex_connector,
            "app_server": codex_app_server,
        }
    except Exception as exc:
        if codex_app_server is not None:
            await asyncio.to_thread(codex_app_server.close)
        codex_app_server = None
        codex_connector = None
        _log_runtime_warning(args, "codex runtime unavailable: %s", exc)

    profile_env = load_env_file(getattr(args, "env_file", None))
    if profile_env:
        logger.info("loaded env file: %s (%s key(s))", args.env_file, len(profile_env))
    try:
        if getattr(args, "claude_bin", None):
            logger.info("using Claude CLI: %s", args.claude_bin)
        claude_provider = ClaudeAgentSdkProvider(
            cwd=connector_cwd,
            default_cwd=getattr(args, "default_workspace_cwd", None),
            exclude_history_cwds=()
            if getattr(args, "default_workspace_cwd", None)
            else (connector_cwd,),
            model=args.model,
            cli_path=getattr(args, "claude_bin", None),
            runtime_profile_id=getattr(args, "runtime_profile_id", DEFAULT_RUNTIME_PROFILE_ID),
            runtime_profile_name=getattr(args, "runtime_profile_name", None),
            env_overrides=profile_env,
            env_file=getattr(args, "env_file", None),
            approval_timeout_seconds=args.approval_timeout,
        )
        runtime_profile = claude_provider.runtime_profile_report()
        logger.info(_runtime_profile_log_line(runtime_profile))
        if _should_warn_missing_claude_env(runtime_profile):
            logger.warning(
                "no exported Claude provider env keys detected; "
                "if your Claude CLI uses a third-party gateway, start the connector from that exported "
                "shell or set --env-file ~/.botsdock/botsdock_connector.env"
            )
        claude_connector = ClaudeCodeConnector(provider=claude_provider, outbound=outbound)
        claude_hello = provider_hello(
            claude_provider,
            connector_version=args.connector_version,
        )
        provider_runtimes.append(
            _provider_runtime_from_hello(
                claude_hello,
                provider=CLAUDE_CODE_AGENT_PROVIDER,
                display_name="Claude Code",
                runtime="claude_agent_sdk",
            )
        )
        runtimes[CLAUDE_CODE_AGENT_PROVIDER] = {
            "provider": CLAUDE_CODE_AGENT_PROVIDER,
            "kind": CLAUDE_CODE_AGENT_PROVIDER,
            "connector": claude_connector,
            "provider_object": claude_provider,
        }
    except Exception as exc:
        _log_runtime_warning(args, "claude runtime unavailable: %s", exc)

    if not runtimes:
        raise ConnectorError("no provider runtimes are available")

    mux = AgentConnectorMux(runtimes)
    hello = _agent_hello(provider_runtimes, connector_version=args.connector_version)
    await websocket.send(json.dumps(hello, separators=(",", ":")))
    accepted = json.loads(await websocket.recv())
    if accepted.get("type") != "connector.accepted":
        raise ConnectorError(f"connector rejected: {accepted}")

    async def accepted_handler(accepted_msg: JsonDict) -> None:
        await save_token(
            accepted=accepted_msg,
            args=args,
            machine_id=machine_id,
            connector_cwd=connector_cwd,
            reconnect_command_fn=reconnect_command,
        )

    await accepted_handler(accepted)

    registration_only = getattr(args, "registration_only", False)
    if registration_only:
        print(
            f"Registration successful!\n"
            f"  Machine: {accepted.get('machine_id') or machine_id}\n"
            f"  Server:  {args.server.rstrip('/')}\n"
            f"  Runtimes: {', '.join(mux.providers()) if mux.providers() else 'none'}\n"
            f"\nRun 'botsdock-connector' to start.",
            file=sys.stderr,
        )
        await mux.stop()
        return

    logger.info(
        "connector accepted: provider=agent runtimes=%s machine=%s session=%s",
        ",".join(mux.providers()), accepted.get("machine_id"), accepted.get("session_id"),
    )

    await _send_initial_runtime_sync(websocket, mux)

    heartbeat_interval = 15
    try:
        raw_interval = accepted.get("heartbeat_interval_seconds") or 15
        heartbeat_interval = max(5, int(raw_interval))
    except (TypeError, ValueError):
        heartbeat_interval = 15

    cancelled = asyncio.Event()
    writer_task = asyncio.create_task(
        outbound_writer(websocket, outbound, tag_provider_fn=_tag_provider_message, provider_tag=None)
    )
    heartbeat_task = asyncio.create_task(
        heartbeat_sender(
            websocket,
            heartbeat_interval_seconds=heartbeat_interval,
            replay_fn=mux.replay_pending_approvals,
            cancelled=cancelled,
        )
    )
    try:
        await message_loop(
            websocket,
            handler=mux.handle_backend_message,
            tag_provider_fn=_tag_provider_message,
            provider_tag=None,
        )
    finally:
        cancelled.set()
        buffered_sender.close()
        writer_task.cancel()
        heartbeat_task.cancel()
        await mux.stop()


# ---------------------------------------------------------------------------
# Connection runner
# ---------------------------------------------------------------------------

async def run_connector_once_for_spec(args: argparse.Namespace, spec: ConnectionSpec) -> None:
    import websockets

    connection_args = _connection_args(args, spec)
    ws_url = backend_ws_url(spec.server, spec.machine_id)
    logger.info(
        "connecting: server=%s machine=%s",
        spec.server.rstrip("/"), spec.machine_id,
    )
    async with websockets.connect(
        ws_url,
        additional_headers={"Authorization": f"Bearer {spec.token}"},
        open_timeout=connection_args.open_timeout,
        ping_interval=20,
        ping_timeout=connection_args.ping_timeout,
    ) as websocket:
        bootstrap = await send_bootstrap(websocket, connection_args)
        provider = bootstrap["provider"]
        logger.info(
            "machine provider: %s machine=%s",
            provider, spec.machine_id,
        )
        await run_agent_provider_session(
            websocket=websocket,
            args=connection_args,
            connector_cwd=spec.cwd,
            machine_id=spec.machine_id,
        )


def is_non_retriable_connector_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "missing connector token" in message
        or "missing machine-id" in message
        or "missing machine id" in message
        or "connector rejected" in message
        or "bootstrap rejected" in message
        or "unsupported machine provider" in message
    )


async def run_connection(args: argparse.Namespace, spec: ConnectionSpec, *, supervised: bool) -> None:
    if not args.reconnect:
        await run_connector_once_for_spec(args, spec)
        return
    attempt = 0
    while True:
        try:
            await run_connector_once_for_spec(args, spec)
            attempt = 0
            logger.info(
                "disconnected; reconnecting %s",
                connection_label(spec),
            )
        except KeyboardInterrupt:
            raise
        except ConnectorError as exc:
            if is_non_retriable_connector_error(exc):
                if supervised:
                    logger.error("stopped %s: %s", connection_label(spec), exc)
                    return
                raise
            attempt += 1
            logger.error("connection failed %s: %s", connection_label(spec), exc)
        except Exception as exc:
            attempt += 1
            logger.error("connection failed %s: %s", connection_label(spec), exc)

        base_delay = max(1.0, float(args.reconnect_initial_delay))
        max_delay = max(base_delay, float(args.reconnect_max_delay))
        delay = min(max_delay, base_delay * (2 ** min(attempt, 6)))
        delay = delay * random.uniform(0.75, 1.25)
        logger.info(
            "reconnecting %s in %.1fs",
            connection_label(spec), delay,
        )
        await asyncio.sleep(delay)


async def run_supervisor(args: argparse.Namespace, specs: list[ConnectionSpec]) -> None:
    logger.info("supervising %s saved connection(s)", len(specs))
    tasks = [
        asyncio.create_task(
            run_connection(args, spec, supervised=len(specs) > 1),
            name=f"botsdock-connector:{spec.machine_id}",
        )
        for spec in specs
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_connector(args: argparse.Namespace) -> None:
    specs = resolve_connection_specs(args)
    if args.token:
        await run_connector_once_for_spec(args, specs[0])
        return
    if len(specs) == 1 and args.machine_id:
        await run_connection(args, specs[0], supervised=False)
        return
    await run_supervisor(args, specs)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if sys.version_info < (3, 10):
        logger.error(
            "botsdock-connector requires Python >= 3.10, found %s.%s.",
            sys.version_info.major, sys.version_info.minor,
        )
        return 1
    args = build_parser().parse_args()

    # Daemon lifecycle commands (synchronous, no asyncio needed).
    if args.command == "start":
        return daemon_start(args)
    if args.command == "stop":
        return daemon_stop()
    if args.command == "restart":
        return daemon_restart(args)
    if args.command == "status":
        return daemon_status()
    if args.command == "register":
        return run_register(args)
    if args.command == "upgrade":
        return run_upgrade(args)

    # Running the connector (foreground or daemon child).
    # Install SIGTERM handler for graceful shutdown.
    install_signal_handlers()
    is_registration = bool(args.token and args.machine_id)
    try:
        asyncio.run(run_connector(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except ConnectorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if is_registration:
            print("Registration failed. Check that --machine-id and --token are correct.", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.error("botsdock connector error: %s", exc, exc_info=True)
        if is_registration:
            print(
                f"Registration failed. Could not connect to backend.\n"
                f"  Details: {exc}\n"
                f"  Check network connectivity and server URL.",
                file=sys.stderr,
            )
        return 1
