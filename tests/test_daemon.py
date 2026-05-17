"""Tests for daemon lifecycle management."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from botsdock_connector.daemon import (
    _build_child_command,
    _is_pid_alive,
    _read_pid_data,
    _remove_pid_data,
    _write_pid_data,
    daemon_status,
    daemon_stop,
    install_signal_handlers,
    log_file_path,
    pid_file_path,
)


def test_pid_file_path_in_home():
    path = pid_file_path()
    assert path.name == "botsdock_connector.pid"
    assert str(Path.home()) in str(path)


def test_log_file_path_defaults_to_home():
    path = log_file_path()
    assert path.name == "botsdock_connector.log"
    assert str(Path.home()) in str(path)


def test_log_file_path_from_env(monkeypatch):
    monkeypatch.setenv("BOTSDOCK_CONNECTOR_LOG_FILE", "/tmp/test_connector.log")
    path = log_file_path()
    assert path == Path("/tmp/test_connector.log").resolve()


def test_is_pid_alive_for_current_process():
    assert _is_pid_alive(os.getpid()) is True


def test_is_pid_alive_for_nonexistent():
    # Use a PID that almost certainly doesn't exist.
    assert _is_pid_alive(99999999) is False


def test_write_and_read_pid_data(tmp_path, monkeypatch):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _remove_pid_data()

    _write_pid_data(12345, "https://www.botsdock.cn", "/tmp/cwd", ["mach_1", "mach_2"])
    data = _read_pid_data()
    assert data is not None
    assert data["pid"] == 12345
    assert data["server"] == "https://www.botsdock.cn"
    assert data["cwd"] == "/tmp/cwd"
    assert data["machine_ids"] == ["mach_1", "mach_2"]
    assert isinstance(data["started_at"], int)


def test_read_pid_data_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _remove_pid_data()
    assert _read_pid_data() is None


def test_read_pid_data_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    pid_file_path().write_text("not json")
    assert _read_pid_data() is None


def test_remove_pid_data_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _remove_pid_data()
    _remove_pid_data()  # Should not raise
    assert not pid_file_path().exists()


def test_daemon_status_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _remove_pid_data()
    ret = daemon_status()
    assert ret == 1
    captured = capsys.readouterr()
    assert "Not running" in captured.err


def test_daemon_status_stale_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _write_pid_data(99999999, "https://www.botsdock.cn", "/tmp", ["mach_1"])
    ret = daemon_status()
    assert ret == 1
    captured = capsys.readouterr()
    assert "Not running" in captured.err


def test_daemon_stop_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _remove_pid_data()
    ret = daemon_stop()
    assert ret == 1
    captured = capsys.readouterr()
    assert "not running" in captured.err


def test_daemon_stop_stale_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("botsdock_connector.daemon._botsdock_dir", lambda: tmp_path)
    _write_pid_data(99999999, "https://www.botsdock.cn", "/tmp", ["mach_1"])
    ret = daemon_stop()
    assert ret == 1
    captured = capsys.readouterr()
    assert "not running" in captured.err


def test_build_child_command_strips_start_and_restart():
    cmd = _build_child_command()
    assert "start" not in cmd
    assert "restart" not in cmd
    # Other subcommands would not normally appear in sys.argv for start/restart.
    assert isinstance(cmd, list) and len(cmd) >= 2


def test_install_signal_handlers():
    import signal
    install_signal_handlers()
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    assert handler is not signal.SIG_DFL
    assert handler is not signal.SIG_IGN
