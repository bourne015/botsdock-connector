"""Shared WebSocket session runner for provider sessions."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from typing import Any, Callable

from . import __version__
from .log import get_logger
from .protocol import CONNECTION_MODE, PROTOCOL_VERSION
from .token_store import (
    ConnectorError,
    save_connector_token,
)

logger = get_logger(__name__)

JsonDict = dict[str, Any]

MessageHandler = Callable[[JsonDict], Any]


async def heartbeat_sender(
    websocket: Any,
    heartbeat_interval_seconds: float = 15,
    *,
    replay_fn: Callable[[], None] | None = None,
    cancelled: asyncio.Event | None = None,
) -> None:
    interval = max(5.0, heartbeat_interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        if cancelled is not None and cancelled.is_set():
            break
        if replay_fn is not None:
            replay_fn()
        await websocket.send(json.dumps({"type": "connector.heartbeat"}, separators=(",", ":")))


async def outbound_writer(
    websocket: Any,
    outbound: asyncio.Queue[JsonDict],
    *,
    tag_provider_fn: Callable[[JsonDict, str], JsonDict] | None = None,
    provider_tag: str | None = None,
) -> None:
    while True:
        message = await outbound.get()
        if tag_provider_fn is not None and provider_tag is not None:
            message = tag_provider_fn(message, provider_tag)
        await websocket.send(json.dumps(message, separators=(",", ":")))


async def message_loop(
    websocket: Any,
    *,
    handler: MessageHandler,
    outbound: asyncio.Queue[JsonDict] | None = None,
    tag_provider_fn: Callable[[JsonDict, str], JsonDict] | None = None,
    provider_tag: str | None = None,
) -> None:
    async for raw in websocket:
        message = json.loads(raw)
        if message.get("type") in {"connector.error", "connection.error"}:
            logger.error(
                "backend error: %s", message.get("error") or message
            )
            continue
        response = await handler(message)
        if response is not None:
            if tag_provider_fn is not None and provider_tag is not None:
                response = tag_provider_fn(response, provider_tag)
            await websocket.send(json.dumps(response, separators=(",", ":")))


async def send_bootstrap(websocket: Any, args: Any) -> JsonDict:
    await websocket.send(
        json.dumps(
            {
                "type": "connector.bootstrap",
                "connector_version": args.connector_version,
                "platform": sys.platform,
                "hostname": socket.gethostname(),
                "protocol_version": PROTOCOL_VERSION,
                "connection_mode": CONNECTION_MODE,
            },
            separators=(",", ":"),
        )
    )
    response = json.loads(await websocket.recv())
    if response.get("type") != "connector.bootstrap":
        raise ConnectorError(f"connector bootstrap rejected: {response}")
    return response


async def save_accepted_token(
    *,
    accepted: JsonDict,
    args: Any,
    machine_id: str,
    connector_cwd: str,
    reconnect_command_fn: Callable[[Any], str],
) -> None:
    new_connector_token = accepted.get("connector_token")
    if not isinstance(new_connector_token, str) or not new_connector_token:
        return
    args.machine_id = machine_id
    args.token = new_connector_token
    provider = accepted.get("provider")
    spec = getattr(args, "connection_spec", None)
    if spec is not None and hasattr(spec, "token"):
        spec.token = new_connector_token
        if isinstance(provider, str) and provider and hasattr(spec, "provider"):
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
    logger.info("connector token saved: %s", token_path)
    logger.info(
        "connector reconnect command: %s",
        reconnect_command_fn(args),
    )
