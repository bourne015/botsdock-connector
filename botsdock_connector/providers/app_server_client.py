"""Local app-server subprocess client for Codex."""

from __future__ import annotations

import json
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..log import get_logger

logger = get_logger(__name__)

JsonDict = dict[str, Any]


class ConnectorError(Exception):
    pass


class AppServerError(ConnectorError):
    pass


def _elapsed_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1000.0


class AppServerProcessClient:
    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        cwd: str | None = None,
        timeout: float = 60,
        on_message: Callable[[JsonDict], None] | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd or str(Path.cwd())
        self.timeout = timeout
        self.on_message = on_message
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, queue.Queue[JsonDict]] = {}
        self._pending_lock = threading.Lock()
        self._stderr_lines: queue.Queue[str] = queue.Queue()
        self._closed = False
        self.proc = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def __del__(self) -> None:
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
        except Exception:
            pass

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        self._closed = True
        if self.proc.poll() is not None:
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        for thread, name in [(self._stdout_thread, "stdout"), (self._stderr_thread, "stderr")]:
            if thread.is_alive():
                thread.join(timeout=3)

    def initialize(self) -> JsonDict:
        response = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "botsdock_codex_remote_console",
                    "title": "Bots Dock Codex Connector",
                    "version": __version__,
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [],
                },
            },
        )
        self.notification("initialized")
        return response

    def request(self, method: str, params: Any | None = None) -> JsonDict:
        request_id = self._allocate_id()
        pending: queue.Queue[JsonDict] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            message: JsonDict = {"id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self.send(message)
            response = pending.get(timeout=self.timeout)
            if "error" in response:
                raise AppServerError(str(response["error"]))
            return response.get("result") or {}
        except queue.Empty as exc:
            raise TimeoutError(f"timed out waiting for app-server response {method}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notification(self, method: str, params: Any | None = None) -> None:
        message: JsonDict = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def send_response(self, request_id: Any, result: JsonDict | None = None, error: JsonDict | None = None) -> None:
        message: JsonDict = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        self.send(message)

    def send(self, message: JsonDict) -> None:
        if self._closed:
            raise AppServerError("app-server client is closed")
        if self.proc.poll() is not None:
            stderr_tail = "\n".join(self.stderr_snapshot()[-20:])
            raise AppServerError(
                f"app-server exited with code {self.proc.returncode}"
                + (f"\nstderr tail:\n{stderr_tail}" if stderr_tail else "")
            )
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def stderr_snapshot(self) -> list[str]:
        lines: list[str] = []
        while True:
            try:
                lines.append(self._stderr_lines.get_nowait())
            except queue.Empty:
                break
        return lines

    def _allocate_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                if self._closed:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {"method": "connector/nonJsonStdout", "params": {"line": line}}
                self._route_message(message)
        except (ValueError, OSError) as exc:
            if not self._closed:
                logger.warning("app-server stdout reader error: %s", exc)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                if self._closed:
                    break
                self._stderr_lines.put(line.rstrip())
        except (ValueError, OSError) as exc:
            if not self._closed:
                logger.warning("app-server stderr reader error: %s", exc)

    def _route_message(self, message: JsonDict) -> None:
        message_id = message.get("id")
        if message_id is not None and ("result" in message or "error" in message):
            with self._pending_lock:
                pending = self._pending.get(message_id)
            if pending is not None:
                pending.put(message)
                return
        if self.on_message is not None:
            self.on_message(message)
