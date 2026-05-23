"""Daemon lifecycle management for botsdock-connector.

Provides start/stop/restart/status operations via PID file and subprocess
self-spawn, without any third-party dependencies.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .log import get_logger

logger = get_logger(__name__)

DAEMON_PID_FILENAME = "botsdock_connector.pid"
DAEMON_LOG_FILENAME = "botsdock_connector.log"
STOP_TIMEOUT_SECONDS = 10
STOP_POLL_INTERVAL = 0.5
START_CHECK_DELAY = 0.5


def _botsdock_dir() -> Path:
    return Path.home() / ".botsdock"


def pid_file_path() -> Path:
    return _botsdock_dir() / DAEMON_PID_FILENAME


def log_file_path() -> Path:
    env_path = os.environ.get("BOTSDOCK_CONNECTOR_LOG_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return _botsdock_dir() / DAEMON_LOG_FILENAME


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def _read_pid_data() -> dict[str, Any] | None:
    path = pid_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_pid_data(pid: int, server: str, cwd: str, machine_ids: list[str]) -> None:
    path = pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "pid": pid,
        "started_at": int(time.time()),
        "server": server,
        "cwd": cwd,
        "machine_ids": machine_ids,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _remove_pid_data() -> None:
    pid_file_path().unlink(missing_ok=True)


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Subprocess command construction
# ---------------------------------------------------------------------------


def _build_child_command() -> list[str]:
    """Reconstruct the CLI command for the daemon child process.

    Takes sys.argv, strips the daemon subcommand ('start', 'restart'),
    and produces ``[python, -m, botsdock_connector, ...original args...]``.
    The child runs the normal foreground connector path.
    """
    cmd = [sys.executable, "-m", "botsdock_connector"]
    # sys.argv[0] is the program name, sys.argv[1:] are the actual args.
    for arg in sys.argv[1:]:
        if arg in {"start", "restart"}:
            continue
        cmd.append(arg)
    return cmd


# ---------------------------------------------------------------------------
# Signal handler (installed in the daemon child / foreground process)
# ---------------------------------------------------------------------------


def install_signal_handlers() -> None:
    """Install SIGTERM handler so the connector shuts down gracefully.

    On SIGTERM (e.g. from ``botsdock-connector stop`` or system shutdown)
    a KeyboardInterrupt is raised, which the existing cleanup path in
    ``main()`` already handles (return code 130, then cleanup via finally).
    """

    def _handle(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def daemon_start(args: Any) -> int:
    """Start the connector as a background daemon process.

    Returns exit code: 0 on success, 1 on failure.
    """
    existing = _read_pid_data()
    if existing is not None:
        pid = existing.get("pid")
        if isinstance(pid, int) and _is_pid_alive(pid):
            print(
                f"botsdock-connector is already running (PID={pid}). "
                f"Use 'botsdock-connector stop' first.",
                file=sys.stderr,
            )
            return 1

    # Clean up any stale PID file from a previous crash.
    _remove_pid_data()

    # Validate configuration early: resolve_connection_specs will raise if
    # no saved connectors or token is missing.
    from .cli import resolve_connection_specs

    specs = resolve_connection_specs(args)
    server = args.server.rstrip("/")
    cwd = str(Path(args.cwd or ".").expanduser().resolve())
    machine_ids = [spec.machine_id for spec in specs]

    # Build the log file path and ensure directory exists.
    log_path = log_file_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = _build_child_command()
    try:
        log_fd = open(str(log_path), "a")
    except OSError as exc:
        print(f"Failed to open log file {log_path}: {exc}", file=sys.stderr)
        return 1

    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
    )
    log_fd.close()

    # Brief wait to catch immediate launch failures.
    time.sleep(START_CHECK_DELAY)
    if proc.poll() is not None:
        print(
            f"botsdock-connector failed to start (exit code {proc.returncode}). "
            f"Check log: {log_path}",
            file=sys.stderr,
        )
        return 1

    _write_pid_data(proc.pid, server, cwd, machine_ids)
    print(
        f"botsdock-connector started (PID={proc.pid}, log={log_path})",
        file=sys.stderr,
    )
    return 0


def daemon_stop() -> int:
    """Stop a running daemon process.

    Returns exit code: 0 on success, 1 if not running.
    """
    data = _read_pid_data()
    if data is None:
        print("botsdock-connector is not running (no PID file).", file=sys.stderr)
        return 1

    pid = data.get("pid")
    if not isinstance(pid, int) or not _is_pid_alive(pid):
        print(
            "botsdock-connector is not running "
            "(PID file present but process not alive).",
            file=sys.stderr,
        )
        _remove_pid_data()
        return 1

    # Send SIGTERM for graceful shutdown.
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"Failed to send signal to PID={pid}: {exc}", file=sys.stderr)
        return 1

    # Wait for the process to exit.
    waited = 0.0
    while waited < STOP_TIMEOUT_SECONDS:
        if not _is_pid_alive(pid):
            _remove_pid_data()
            print(f"botsdock-connector stopped (PID={pid}).", file=sys.stderr)
            return 0
        time.sleep(STOP_POLL_INTERVAL)
        waited += STOP_POLL_INTERVAL

    # Graceful shutdown timed out; force kill.
    print(
        f"botsdock-connector did not stop within {STOP_TIMEOUT_SECONDS}s, "
        f"force killing PID={pid}.",
        file=sys.stderr,
    )
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        print(f"Failed to force kill PID={pid}: {exc}", file=sys.stderr)
    _remove_pid_data()
    return 0


def daemon_restart(args: Any) -> int:
    """Restart the daemon: stop the current one and start a new one."""
    print("botsdock-connector restarting...", file=sys.stderr)
    daemon_stop()
    # Short pause to let the old process fully release resources.
    time.sleep(0.5)
    return daemon_start(args)


def daemon_status() -> int:
    """Print the daemon status.

    Returns exit code: 0 if running, 1 if not running.
    """
    data = _read_pid_data()
    if data is None:
        print("Not running (no PID file).", file=sys.stderr)
        return 1

    pid = data.get("pid")
    if not isinstance(pid, int):
        print("Not running (invalid PID file).", file=sys.stderr)
        return 1

    if not _is_pid_alive(pid):
        print(
            "Not running (PID file present but process not alive).",
            file=sys.stderr,
        )
        return 1

    started_at = data.get("started_at")
    if isinstance(started_at, (int, float)):
        elapsed = int(time.time() - started_at)
        hours, rem = divmod(elapsed, 3600)
        mins, secs = divmod(rem, 60)
        if hours:
            uptime = f"{hours}h {mins}m {secs}s"
        elif mins:
            uptime = f"{mins}m {secs}s"
        else:
            uptime = f"{secs}s"
    else:
        uptime = "unknown"

    machine_ids = data.get("machine_ids", [])

    print(f"Running (PID={pid}, uptime={uptime})", file=sys.stderr)
    if machine_ids:
        print(f"Machines: {', '.join(machine_ids)}", file=sys.stderr)
    print(f"Log: {log_file_path()}", file=sys.stderr)
    return 0
