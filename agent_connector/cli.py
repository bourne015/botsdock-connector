from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import shlex
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .providers.claude_agent_sdk import ClaudeAgentSdkProvider
from .providers.codex_app_server import (
    AppServerProcessClient,
    BufferedBackendSender,
    CodexConnector,
    _version_label,
)
from .token_store import (
    DEFAULT_SERVER,
    ConnectorError,
    SavedConnector,
    backend_ws_url,
    load_connector_token,
    load_saved_connectors,
    save_connector_token,
)


JsonDict = dict[str, Any]
CODEX_AGENT_PROVIDER = "codex"
CLAUDE_CODE_AGENT_PROVIDER = "claude_code"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="botsdock-agent-connector",
        description="Run the BotsDock agent connector",
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("BOTSDOCK_AGENT_SERVER", os.environ.get("BOTSDOCK_CODEX_SERVER", DEFAULT_SERVER)),
        help=f"Backend base URL. Defaults to BOTSDOCK_AGENT_SERVER, BOTSDOCK_CODEX_SERVER, or {DEFAULT_SERVER}",
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
        default=os.environ.get("BOTSDOCK_AGENT_PROFILE")
        or os.environ.get("BOTSDOCK_CLAUDE_PROFILE"),
        help="Local runtime profile id for provider-specific CLI/env settings. Defaults to 'default'.",
    )
    parser.add_argument(
        "--runtime-profile-name",
        default=os.environ.get("BOTSDOCK_AGENT_PROFILE_NAME")
        or os.environ.get("BOTSDOCK_CLAUDE_PROFILE_NAME"),
        help="Optional display name for the local runtime profile.",
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("BOTSDOCK_AGENT_ENV_FILE"),
        help="Local env file for provider credentials. Defaults to a profile-specific ~/.botsdock/agent_connector.<profile>.env or ~/.botsdock/agent_connector.env when present.",
    )
    parser.add_argument("--model", default=None, help="provider model override")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--claude-bin",
        default=os.environ.get("BOTSDOCK_CLAUDE_BIN")
        or os.environ.get("CLAUDE_CODE_BIN")
        or shutil.which("claude"),
        help="Claude Code CLI path. Defaults to BOTSDOCK_CLAUDE_BIN, CLAUDE_CODE_BIN, or the claude binary on PATH; otherwise the Agent SDK chooses its bundled CLI.",
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
    parser.set_defaults(reconnect=True)
    return parser


def normalize_runtime_profile_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_RUNTIME_PROFILE_ID


def default_env_file(runtime_profile_id: str | None = None) -> str | None:
    configured = os.environ.get("BOTSDOCK_AGENT_ENV_FILE")
    if configured:
        return configured
    profile_id = normalize_runtime_profile_id(runtime_profile_id)
    if profile_id != DEFAULT_RUNTIME_PROFILE_ID:
        profile_path = Path.home() / ".botsdock" / f"agent_connector.{profile_id}.env"
        if profile_path.exists():
            return str(profile_path)
    path = Path.home() / ".botsdock" / "agent_connector.env"
    return str(path) if path.exists() else None


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
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
    return loaded


def provider_hello(provider: ClaudeAgentSdkProvider, *, connector_version: str) -> JsonDict:
    hello: JsonDict = {
        "type": "connector.hello",
        "provider": provider.name,
        "connector_version": connector_version,
        "platform": sys.platform,
        "hostname": socket.gethostname(),
        "protocol_version": "0.1",
        "connection_mode": "remote_ws",
        "capabilities": [
            "app_server.thread_start",
            "app_server.thread_resume",
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


def empty_thread_sync_report() -> JsonDict:
    return {"type": "thread.sync", "threads": [], "workspaces": []}


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
    thread_id = payload.get("thread_id") or request.get("thread_id")
    turn_id = payload.get("turn_id") or request.get("turn_id")
    return {
        "type": "app_server.event",
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
            "thread_id": thread_id,
            "turn_id": turn_id,
            "provider_event_id": envelope.provider_event_id,
            "provider_thread_id": envelope.provider_thread_id,
            "provider_turn_id": envelope.provider_turn_id,
            "provider_session_id": envelope.provider_session_id,
        },
        "raw_provider_event": envelope.raw_event,
    }


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
                await task
            except asyncio.CancelledError:
                pass
        await self.provider.stop()


def reconnect_command(args: argparse.Namespace) -> str:
    parts = ["botsdock-agent-connector"]
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
    if token:
        raise ConnectorError("missing machine-id for registration token connection")

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


async def resolve_connection_args(args: argparse.Namespace) -> tuple[str, str, str]:
    specs = resolve_connection_specs(args)
    if len(specs) > 1:
        raise ConnectorError(
            "multiple saved connectors found; call resolve_connection_specs for supervisor mode"
        )
    spec = specs[0]
    return spec.cwd, spec.machine_id, spec.token


def connection_label(spec: ConnectionSpec) -> str:
    provider = f" provider={spec.provider}" if spec.provider else ""
    profile = (
        f" profile={spec.runtime_profile_id}"
        if spec.runtime_profile_id != DEFAULT_RUNTIME_PROFILE_ID
        else ""
    )
    return f"machine={spec.machine_id}{provider}{profile}"


async def run_connector_once_for_spec(args: argparse.Namespace, spec: ConnectionSpec) -> None:
    import websockets

    connection_args = _connection_args(args, spec)
    ws_url = backend_ws_url(spec.server, spec.machine_id)
    print(
        f"agent connector connecting: server={spec.server.rstrip('/')} machine={spec.machine_id}",
        file=sys.stderr,
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
        print(
            f"agent connector selected provider: {provider} machine={spec.machine_id}",
            file=sys.stderr,
        )
        if provider == CODEX_AGENT_PROVIDER:
            await run_codex_provider_session(
                websocket=websocket,
                args=connection_args,
                connector_cwd=spec.cwd,
                machine_id=spec.machine_id,
            )
            return
        if provider == CLAUDE_CODE_AGENT_PROVIDER:
            await run_claude_provider_session(
                websocket=websocket,
                args=connection_args,
                connector_cwd=spec.cwd,
                machine_id=spec.machine_id,
            )
            return
        raise ConnectorError(f"unsupported machine provider: {provider}")


async def run_connection(args: argparse.Namespace, spec: ConnectionSpec, *, supervised: bool) -> None:
    if not args.reconnect:
        await run_connector_once_for_spec(args, spec)
        return
    attempt = 0
    while True:
        try:
            await run_connector_once_for_spec(args, spec)
            attempt = 0
            print(
                f"agent connector disconnected; reconnecting {connection_label(spec)}",
                file=sys.stderr,
            )
        except KeyboardInterrupt:
            raise
        except ConnectorError as exc:
            if is_non_retriable_connector_error(exc):
                if supervised:
                    print(
                        f"agent connector stopped {connection_label(spec)}: {exc}",
                        file=sys.stderr,
                    )
                    return
                raise
            attempt += 1
            print(
                f"agent connector connection failed {connection_label(spec)}: {exc}",
                file=sys.stderr,
            )
        except Exception as exc:
            attempt += 1
            print(
                f"agent connector connection failed {connection_label(spec)}: {exc}",
                file=sys.stderr,
            )

        base_delay = max(1.0, float(args.reconnect_initial_delay))
        max_delay = max(base_delay, float(args.reconnect_max_delay))
        delay = min(max_delay, base_delay * (2 ** min(attempt, 6)))
        delay = delay * random.uniform(0.75, 1.25)
        print(
            f"agent connector reconnecting {connection_label(spec)} in {delay:.1f}s",
            file=sys.stderr,
        )
        await asyncio.sleep(delay)


async def run_supervisor(args: argparse.Namespace, specs: list[ConnectionSpec]) -> None:
    print(f"agent connector supervising {len(specs)} saved connection(s)", file=sys.stderr)
    tasks = [
        asyncio.create_task(
            run_connection(args, spec, supervised=len(specs) > 1),
            name=f"agent-connector:{spec.machine_id}",
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


async def send_bootstrap(websocket: Any, args: argparse.Namespace) -> JsonDict:
    await websocket.send(
        json.dumps(
            {
                "type": "connector.bootstrap",
                "connector_version": args.connector_version,
                "platform": sys.platform,
                "hostname": socket.gethostname(),
                "protocol_version": "0.1",
                "connection_mode": "remote_ws",
            },
            separators=(",", ":"),
        )
    )
    response = json.loads(await websocket.recv())
    if response.get("type") != "connector.bootstrap":
        raise ConnectorError(f"connector bootstrap rejected: {response}")
    provider = response.get("provider")
    if provider not in {CODEX_AGENT_PROVIDER, CLAUDE_CODE_AGENT_PROVIDER}:
        raise ConnectorError(f"unsupported machine provider: {provider}")
    return response


async def save_accepted_token(
    *,
    accepted: JsonDict,
    args: argparse.Namespace,
    machine_id: str,
    connector_cwd: str,
) -> None:
    new_connector_token = accepted.get("connector_token")
    if not isinstance(new_connector_token, str) or not new_connector_token:
        return
    args.machine_id = machine_id
    args.token = new_connector_token
    provider = accepted.get("provider")
    spec = getattr(args, "connection_spec", None)
    if isinstance(spec, ConnectionSpec):
        spec.token = new_connector_token
        if isinstance(provider, str) and provider:
            spec.provider = provider
    token_path = save_connector_token(
        server_url=args.server,
        machine_id=machine_id,
        token=new_connector_token,
        provider=provider if isinstance(provider, str) else None,
        runtime_profile={
            "id": getattr(args, "runtime_profile_id", None)
            or getattr(args, "runtime_profile", None),
            "display_name": getattr(args, "runtime_profile_name", None),
            "env_file": getattr(args, "env_file", None),
            "model": getattr(args, "model", None),
            "claude_bin": getattr(args, "claude_bin", None),
        },
        cwd=connector_cwd,
    )
    print(f"agent connector token saved: {token_path}", file=sys.stderr)
    print(f"agent connector reconnect command: {reconnect_command(args)}", file=sys.stderr)


async def run_claude_provider_session(
    *,
    websocket: Any,
    args: argparse.Namespace,
    connector_cwd: str,
    machine_id: str,
) -> None:
    outbound: asyncio.Queue[JsonDict] = asyncio.Queue()
    profile_env = load_env_file(getattr(args, "env_file", None))
    if profile_env:
        print(
            f"agent connector loaded env file: {args.env_file} ({len(profile_env)} key(s))",
            file=sys.stderr,
        )
    if getattr(args, "claude_bin", None):
        print(
            f"agent connector using Claude CLI: {args.claude_bin}",
            file=sys.stderr,
        )
    provider = ClaudeAgentSdkProvider(
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
    connector = ClaudeCodeConnector(provider=provider, outbound=outbound)
    hello = provider_hello(provider, connector_version=args.connector_version)
    await websocket.send(json.dumps(hello, separators=(",", ":")))
    accepted = json.loads(await websocket.recv())
    if accepted.get("type") != "connector.accepted":
        raise ConnectorError(f"connector rejected: {accepted}")
    await save_accepted_token(
        accepted=accepted,
        args=args,
        machine_id=machine_id,
        connector_cwd=connector_cwd,
    )
    print(
        f"agent connector accepted: provider=claude_code machine={accepted.get('machine_id')} session={accepted.get('session_id')}",
        file=sys.stderr,
    )
    if getattr(args, "registration_only", False):
        print(
            "agent connector registration saved; run `botsdock-agent-connector` to start all saved connections",
            file=sys.stderr,
        )
        return
    thread_sync = provider.thread_sync_report()
    if thread_sync.get("workspaces"):
        await websocket.send(
            json.dumps(
                {"type": "workspace.report", "workspaces": thread_sync["workspaces"]},
                separators=(",", ":"),
            )
        )
    await websocket.send(json.dumps(thread_sync, separators=(",", ":")))

    async def outbound_writer() -> None:
        while True:
            await websocket.send(json.dumps(await outbound.get(), separators=(",", ":")))

    async def heartbeat_sender() -> None:
        interval = accepted.get("heartbeat_interval_seconds") or 15
        try:
            interval_seconds = max(5, int(interval))
        except (TypeError, ValueError):
            interval_seconds = 15
        while True:
            await asyncio.sleep(interval_seconds)
            await websocket.send(json.dumps({"type": "connector.heartbeat"}, separators=(",", ":")))

    writer_task = asyncio.create_task(outbound_writer())
    heartbeat_task = asyncio.create_task(heartbeat_sender())
    try:
        async for raw in websocket:
            message = json.loads(raw)
            if message.get("type") in {"connector.error", "connection.error"}:
                print(
                    f"agent connector backend error: {message.get('error') or message}",
                    file=sys.stderr,
                )
                continue
            response = await connector.handle_backend_message(message)
            if response is not None:
                await websocket.send(json.dumps(response, separators=(",", ":")))
    finally:
        writer_task.cancel()
        heartbeat_task.cancel()
        await connector.stop()


async def run_codex_provider_session(
    *,
    websocket: Any,
    args: argparse.Namespace,
    connector_cwd: str,
    machine_id: str,
) -> None:
    loop = asyncio.get_running_loop()
    outbound: asyncio.Queue[JsonDict] = asyncio.Queue()
    buffered_sender = BufferedBackendSender(
        loop=loop,
        outbound=outbound,
        flush_interval=args.delta_flush_interval,
        max_chars=args.delta_flush_chars,
    )

    app_server: AppServerProcessClient | None = None
    connector: CodexConnector | None = None
    try:
        def on_appserver_message(message: JsonDict) -> None:
            if connector is not None:
                connector.handle_appserver_message(message)

        app_server = AppServerProcessClient(
            codex_bin=args.codex_bin,
            cwd=connector_cwd,
            timeout=args.timeout,
            on_message=on_appserver_message,
        )
        connector = CodexConnector(app_server=app_server, cwd=connector_cwd, model=args.model)
        connector.bind_backend_sender(buffered_sender.send)
        init_result = connector.initialize_app_server()
        hello = connector.hello(connector_version=args.connector_version)
        hello["connector_version"] = args.connector_version
        hello["app_server"]["version"] = _version_label(init_result.get("userAgent"))
        await websocket.send(json.dumps(hello, separators=(",", ":")))
        accepted = json.loads(await websocket.recv())
        if accepted.get("type") != "connector.accepted":
            raise ConnectorError(f"connector rejected: {accepted}")
        await save_accepted_token(
            accepted=accepted,
            args=args,
            machine_id=machine_id,
            connector_cwd=connector_cwd,
        )
        print(
            f"agent connector accepted: provider=codex machine={accepted.get('machine_id')} session={accepted.get('session_id')}",
            file=sys.stderr,
        )
        if getattr(args, "registration_only", False):
            print(
                "agent connector registration saved; run `botsdock-agent-connector` to start all saved connections",
                file=sys.stderr,
            )
            return
        thread_sync = connector.thread_sync_report()
        if thread_sync.get("workspaces"):
            await websocket.send(
                json.dumps(
                    {"type": "workspace.report", "workspaces": thread_sync["workspaces"]},
                    separators=(",", ":"),
                )
            )
        await websocket.send(json.dumps(thread_sync, separators=(",", ":")))

        async def outbound_writer() -> None:
            while True:
                await websocket.send(json.dumps(await outbound.get(), separators=(",", ":")))

        async def heartbeat_sender() -> None:
            interval = accepted.get("heartbeat_interval_seconds") or 15
            try:
                interval_seconds = max(5, int(interval))
            except (TypeError, ValueError):
                interval_seconds = 15
            while True:
                await asyncio.sleep(interval_seconds)
                connector.replay_pending_approvals()
                await websocket.send(json.dumps({"type": "connector.heartbeat"}, separators=(",", ":")))

        writer_task = asyncio.create_task(outbound_writer())
        heartbeat_task = asyncio.create_task(heartbeat_sender())
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if message.get("type") in {"connector.error", "connection.error"}:
                    print(
                        f"agent connector backend error: {message.get('error') or message}",
                        file=sys.stderr,
                    )
                    continue
                response = await asyncio.to_thread(connector.handle_backend_message, message)
                if response is not None:
                    await websocket.send(json.dumps(response, separators=(",", ":")))
        finally:
            buffered_sender.flush_all()
            writer_task.cancel()
            heartbeat_task.cancel()
    finally:
        if app_server is not None:
            app_server.close()


async def run_connector_once(args: argparse.Namespace) -> None:
    specs = resolve_connection_specs(args)
    if len(specs) != 1:
        raise ConnectorError("run_connector_once requires a single connection spec")
    await run_connector_once_for_spec(args, specs[0])


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


async def run_connector(args: argparse.Namespace) -> None:
    specs = resolve_connection_specs(args)
    if args.token:
        await run_connector_once_for_spec(args, specs[0])
        return
    if len(specs) == 1 and (args.machine_id or args.token):
        await run_connection(args, specs[0], supervised=False)
        return
    await run_supervisor(args, specs)


def main() -> int:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_connector(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"agent connector error: {exc}", file=sys.stderr)
        return 1
