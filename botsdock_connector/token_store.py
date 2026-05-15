from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]

TOKEN_STORE_FILE = ".botsdock_connector.json"
LEGACY_TOKEN_STORE_FILES = (
    ".botsdock_agent_connector.json",
    ".codex_connector.json",
    ".botsdock_codex_connector.json",
)
DEFAULT_SERVER = "https://www.botsdock.cn"
LEGACY_SERVER_URLS = {"https://botsdock.com", "https://www.botsdock.com"}


class ConnectorError(Exception):
    pass


@dataclass(frozen=True)
class SavedConnector:
    server: str
    machine_id: str
    token: str
    provider: str | None = None
    runtime_profile_id: str | None = None
    runtime_profile_name: str | None = None
    env_file: str | None = None
    model: str | None = None
    claude_bin: str | None = None


def backend_ws_url(server_url: str, machine_id: str) -> str:
    base = server_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/v1/console/connect?machine_id={machine_id}"


def token_store_path(cwd: str | None = None) -> Path:
    # Keep the cwd argument for older callers; new writes always go to the user home.
    del cwd
    return Path.home().expanduser().resolve() / TOKEN_STORE_FILE


def read_token_store_path(cwd: str | None = None) -> Path:
    path = token_store_path()
    if path.exists():
        return path
    bases: list[Path] = [path.parent]
    legacy_base = (
        Path(cwd).expanduser().resolve()
        if cwd
        else Path.cwd().expanduser().resolve()
    )
    if legacy_base not in bases:
        bases.append(legacy_base)
    for base in bases:
        for filename in (TOKEN_STORE_FILE, *LEGACY_TOKEN_STORE_FILES):
            legacy = base / filename
            if legacy.exists():
                return legacy
    return path


def token_store_key(server_url: str, machine_id: str) -> str:
    normalized_server = normalized_server_url(server_url)
    return hashlib.sha256(f"{normalized_server}\n{machine_id}".encode("utf-8")).hexdigest()


def normalized_server_url(server_url: str) -> str:
    return server_url.rstrip("/")


def load_token_store(path: Path) -> JsonDict:
    if not path.exists():
        return {"version": 1, "connectors": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"failed to read connector token store {path}: {exc}") from exc
    if not isinstance(data, dict):
        return {"version": 1, "connectors": {}}
    connectors = data.get("connectors")
    if not isinstance(connectors, dict):
        data["connectors"] = {}
    data.setdefault("version", 1)
    return data


def load_connector_token(*, server_url: str, machine_id: str, cwd: str | None = None) -> str | None:
    path = read_token_store_path(cwd)
    data = load_token_store(path)
    connectors = data.get("connectors", {})
    entry = connectors.get(token_store_key(server_url, machine_id))
    if not isinstance(entry, dict) and normalized_server_url(server_url) == DEFAULT_SERVER:
        for candidate in connectors.values():
            if not isinstance(candidate, dict):
                continue
            if (
                candidate.get("machine_id") == machine_id
                and candidate.get("server") in LEGACY_SERVER_URLS
            ):
                entry = candidate
                break
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    return token if isinstance(token, str) and token else None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _connector_from_entry(entry: Any, *, server_url: str | None = None) -> SavedConnector | None:
    if not isinstance(entry, dict):
        return None
    machine_id = entry.get("machine_id")
    token = entry.get("token")
    server = entry.get("server") or server_url
    provider = entry.get("provider")
    runtime_profile = entry.get("runtime_profile")
    if not isinstance(runtime_profile, dict):
        runtime_profile = {}
    if (
        isinstance(machine_id, str)
        and machine_id
        and isinstance(token, str)
        and token
        and isinstance(server, str)
        and server
    ):
        return SavedConnector(
            server=normalized_server_url(server),
            machine_id=machine_id,
            token=token,
            provider=provider if isinstance(provider, str) and provider else None,
            runtime_profile_id=_optional_str(
                runtime_profile.get("id") or entry.get("runtime_profile_id")
            ),
            runtime_profile_name=_optional_str(
                runtime_profile.get("display_name") or entry.get("runtime_profile_name")
            ),
            env_file=_optional_str(runtime_profile.get("env_file") or entry.get("env_file")),
            model=_optional_str(runtime_profile.get("model") or entry.get("model")),
            claude_bin=_optional_str(runtime_profile.get("claude_bin") or entry.get("claude_bin")),
        )
    return None


def load_saved_connectors(*, server_url: str, cwd: str | None = None) -> list[SavedConnector]:
    path = read_token_store_path(cwd)
    data = load_token_store(path)
    normalized_server = normalized_server_url(server_url)
    matches: list[SavedConnector] = []
    seen: set[tuple[str, str]] = set()
    for entry in data.get("connectors", {}).values():
        connector = _connector_from_entry(entry, server_url=normalized_server)
        if connector is None or connector.server != normalized_server:
            continue
        key = (connector.server, connector.machine_id)
        if key not in seen:
            seen.add(key)
            matches.append(connector)
    if not matches and normalized_server == DEFAULT_SERVER:
        for entry in data.get("connectors", {}).values():
            connector = _connector_from_entry(entry)
            if connector is None:
                continue
            if connector.server not in LEGACY_SERVER_URLS:
                continue
            connector = SavedConnector(
                server=normalized_server,
                machine_id=connector.machine_id,
                token=connector.token,
                provider=connector.provider,
                runtime_profile_id=connector.runtime_profile_id,
                runtime_profile_name=connector.runtime_profile_name,
                env_file=connector.env_file,
                model=connector.model,
                claude_bin=connector.claude_bin,
            )
            key = (connector.server, connector.machine_id)
            if key not in seen:
                seen.add(key)
                matches.append(connector)
    return matches


def load_single_saved_connector(*, server_url: str, cwd: str | None = None) -> tuple[str, str] | None:
    path = read_token_store_path(cwd)
    matches = load_saved_connectors(server_url=server_url, cwd=cwd)
    if not matches:
        return None
    if len(matches) > 1:
        raise ConnectorError(
            f"multiple saved connectors found in {path}; pass --machine-id to choose one"
        )
    connector = matches[0]
    return connector.machine_id, connector.token


def save_connector_token(
    *,
    server_url: str,
    machine_id: str,
    token: str,
    provider: str | None,
    runtime_profile: dict[str, Any] | None = None,
    cwd: str | None = None,
) -> Path:
    read_path = read_token_store_path(cwd)
    path = token_store_path(cwd)
    data = load_token_store(read_path)
    connectors = data.setdefault("connectors", {})
    entry: JsonDict = {
        "server": normalized_server_url(server_url),
        "machine_id": machine_id,
        "provider": provider,
        "token": token,
    }
    if runtime_profile:
        saved_profile: JsonDict = {}
        for key in (
            "id",
            "display_name",
            "env_file",
            "model",
            "claude_bin",
        ):
            value = _optional_str(runtime_profile.get(key))
            if value is not None:
                saved_profile[key] = value
        if saved_profile:
            entry["runtime_profile"] = saved_profile
    connectors[token_store_key(server_url, machine_id)] = entry
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    tmp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
