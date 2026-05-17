"""Device-initiated machine registration flow.

The connector generates a machine_id locally, announces itself to the
backend to get a short registration code, displays it to the user, then
polls the backend until the user confirms the code on the website.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.error import URLError

from .log import get_logger
from .token_store import (
    DEFAULT_SERVER,
    load_saved_connectors,
    save_connector_token,
)

logger = get_logger(__name__)

JSON_HEADERS = {"Content-Type": "application/json"}
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 1800  # 30 minutes


def _generate_machine_id() -> str:
    return f"mach_{uuid.uuid4().hex}"


def _http_post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(url, data=data, headers=JSON_HEADERS, method="POST")
    with urllib_request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _http_get_json(url: str) -> dict[str, Any]:
    with urllib_request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _announce(server_url: str, machine_id: str) -> dict[str, Any]:
    url = f"{server_url.rstrip('/')}/v1/console/machines/announce"
    return _http_post_json(url, {
        "machine_id": machine_id,
        "hostname": socket.gethostname(),
        "platform": sys.platform,
    })


def _poll_status(server_url: str, code: str) -> dict[str, Any]:
    url = f"{server_url.rstrip('/')}/v1/console/machines/registration-status?code={code}"
    return _http_get_json(url)


def run_register(args: Any) -> int:
    """Run the device-initiated registration flow.

    Returns exit code: 0 on success, 1 on failure.
    """
    server_url = getattr(args, "server", DEFAULT_SERVER).rstrip("/")
    renew = getattr(args, "renew", False)
    cwd = str(Path(getattr(args, "cwd", ".") or ".").expanduser().resolve())

    # Check if already registered (has connector_token).
    if not renew:
        saved = load_saved_connectors(server_url=server_url, cwd=cwd)
        if saved:
            print(
                "Already registered. Use 'botsdock-connector start' to run the daemon.",
                file=sys.stderr,
            )
            return 0

    # Generate or load machine_id.
    # For renew: use existing machine_id from token store keyed by server_url.
    machine_id = ""
    if renew:
        saved = load_saved_connectors(server_url=server_url, cwd=cwd)
        # With renew, we may not have a connector_token but may have a previous
        # machine_id in the store. Try to find it.
        machine_id = getattr(args, "machine_id", None) or ""
        if not machine_id and saved:
            machine_id = saved[0].machine_id
    if not machine_id:
        machine_id = _generate_machine_id()

    print(f"Machine ID: {machine_id}", file=sys.stderr)

    # Announce to backend.
    try:
        result = _announce(server_url, machine_id)
    except URLError as exc:
        print(f"Failed to connect to {server_url}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Registration failed: {exc}", file=sys.stderr)
        return 1

    code = result.get("registration_code")
    if not code:
        print(f"Unexpected response: {result}", file=sys.stderr)
        return 1

    expires_at = result.get("expires_at")
    print(file=sys.stderr)
    print(f"Registration code:  {code}", file=sys.stderr)
    if expires_at:
        remaining = max(0, int(expires_at - time.time()))
        mins, secs = divmod(remaining, 60)
        print(f"Valid for:          {mins}m {secs}s", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"Open {server_url} in your browser, enter this code to link this machine.",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("Waiting for confirmation...", file=sys.stderr)

    # Poll for confirmation.
    started = time.monotonic()
    while (time.monotonic() - started) < POLL_TIMEOUT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            status = _poll_status(server_url, code)
        except Exception:
            continue

        if status.get("status") == "confirmed":
            connector_token = status.get("connector_token")
            if not connector_token:
                print("Registration confirmed but no token received. Try again.", file=sys.stderr)
                return 1

            # Save the token.
            save_connector_token(
                server_url=server_url,
                machine_id=machine_id,
                token=connector_token,
                provider=None,
                runtime_profile={
                    "id": getattr(args, "runtime_profile_id", None)
                    or getattr(args, "runtime_profile", None),
                    "display_name": getattr(args, "runtime_profile_name", None),
                    "env_file": getattr(args, "env_file", None),
                    "model": getattr(args, "model", None),
                    "claude_bin": getattr(args, "claude_bin", None),
                },
                cwd=cwd,
            )
            print(file=sys.stderr)
            print("Machine registered!", file=sys.stderr)
            print(file=sys.stderr)
            print("Run 'botsdock-connector start' to start the daemon.", file=sys.stderr)
            return 0

    print(file=sys.stderr)
    print("Registration timed out. Run 'botsdock-connector register' to try again.", file=sys.stderr)
    return 1
