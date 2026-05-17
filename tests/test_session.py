"""Tests for shared session runner."""

from __future__ import annotations

import asyncio
import json

import pytest

from botsdock_connector.session import (
    heartbeat_sender,
    outbound_writer,
    message_loop,
    send_bootstrap,
)
from botsdock_connector.protocol import PROTOCOL_VERSION, CONNECTION_MODE
from botsdock_connector.token_store import ConnectorError


class MockWebSocket:
    def __init__(self, sent_messages=None):
        self.sent = sent_messages if sent_messages is not None else []
        self._recv_queue = asyncio.Queue()
        self._closed = False

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        return await self._recv_queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_heartbeat_clamps_minimum_interval():
    """Heartbeat sender enforces a minimum 5-second interval for safety."""
    ws = MockWebSocket()
    cancelled = asyncio.Event()
    task = asyncio.create_task(heartbeat_sender(ws, 0.01, cancelled=cancelled))
    await asyncio.sleep(0.05)
    cancelled.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    heartbeats = [m for m in ws.sent if "connector.heartbeat" in m]
    assert len(heartbeats) == 0


@pytest.mark.asyncio
async def test_outbound_writer_forwards_messages():
    ws = MockWebSocket()
    queue = asyncio.Queue()
    task = asyncio.create_task(outbound_writer(ws, queue))
    await queue.put({"type": "test", "data": "hello"})
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(ws.sent) == 1
    parsed = json.loads(ws.sent[0])
    assert parsed["type"] == "test"
    assert parsed["data"] == "hello"


@pytest.mark.asyncio
async def test_message_loop_handles_messages():
    ws = MockWebSocket()
    await ws._recv_queue.put(json.dumps({"type": "test_request", "request_id": "1"}))
    await ws._recv_queue.put(
        json.dumps({"type": "connector.error", "error": {"code": "TEST", "message": "boom"}})
    )

    handled = []

    async def handler(message):
        if message["type"] == "test_request":
            handled.append(message)
            return {"type": "response", "request_id": message["request_id"]}
        return None

    # Run message_loop with a timeout
    async def run_loop():
        await message_loop(ws, handler=handler)
    task = asyncio.create_task(run_loop())

    # Let it process some messages
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass

    assert len(handled) == 1
    # Error message should be logged but not passed to handler
    responses = [m for m in ws.sent if "response" in m]
    assert len(responses) == 1


@pytest.mark.asyncio
async def test_send_bootstrap_success():
    ws = MockWebSocket()
    resp = {
        "type": "connector.bootstrap",
        "provider": "agent",
    }
    await ws._recv_queue.put(json.dumps(resp))

    class Args:
        connector_version = "0.1.8"

    result = await send_bootstrap(ws, Args())
    assert result["type"] == "connector.bootstrap"
    assert result["provider"] == "agent"

    bootstrap_msg = json.loads(ws.sent[0])
    assert bootstrap_msg["type"] == "connector.bootstrap"
    assert bootstrap_msg["protocol_version"] == PROTOCOL_VERSION
    assert bootstrap_msg["connection_mode"] == CONNECTION_MODE


@pytest.mark.asyncio
async def test_send_bootstrap_rejection():
    ws = MockWebSocket()
    await ws._recv_queue.put(json.dumps({"type": "error", "error": "invalid token"}))

    class Args:
        connector_version = "0.1.8"

    with pytest.raises(ConnectorError, match="bootstrap rejected"):
        await send_bootstrap(ws, Args())


def test_protocol_constants():
    assert PROTOCOL_VERSION == "0.1"
    assert CONNECTION_MODE == "remote_ws"


# SessionConfig removed — unused abstraction
