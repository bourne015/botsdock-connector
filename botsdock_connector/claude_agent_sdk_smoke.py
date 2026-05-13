#!/usr/bin/env python3
"""Smoke-test Claude Agent SDK integration for the remote connector."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {
            str(k): _jsonable(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a minimal Claude Agent SDK smoke test")
    parser.add_argument("--cwd", default=".", help="working directory for Claude Code")
    parser.add_argument("--model", default=None)
    parser.add_argument("--claude-bin", default=None, help="Claude Code CLI path")
    parser.add_argument("--prompt", default="Reply with one short sentence.")
    parser.add_argument("--max-events", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument(
        "--check-import",
        action="store_true",
        help="Only verify the package can be imported and print basic metadata",
    )
    return parser


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import claude_agent_sdk
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError as err:
        return {
            "ok": False,
            "stage": "import",
            "error": str(err),
            "hint": "Reinstall the connector with `python3 -m pip install -e .`.",
        }

    version = getattr(claude_agent_sdk, "__version__", None)
    if args.check_import:
        return {
            "ok": True,
            "stage": "import",
            "package": "claude-agent-sdk",
            "module": "claude_agent_sdk",
            "version": version,
        }

    options_kwargs: dict[str, Any] = {
        "cwd": str(Path(args.cwd).expanduser().resolve()),
        "system_prompt": {"type": "preset", "preset": "claude_code"},
        "tools": {"type": "preset", "preset": "claude_code"},
        "allowed_tools": ["Read", "Glob", "Grep", "LS"],
        "permission_mode": "dontAsk",
        "setting_sources": ["user", "project", "local"],
        "env": {"CLAUDE_AGENT_SDK_CLIENT_APP": "botsdock-connector-smoke"},
    }
    if args.model:
        options_kwargs["model"] = args.model
    if args.claude_bin:
        options_kwargs["cli_path"] = args.claude_bin
    options = ClaudeAgentOptions(**options_kwargs)
    events: list[dict[str, Any]] = []

    async def collect() -> None:
        async for message in query(prompt=args.prompt, options=options):
            events.append(
                {
                    "type": type(message).__name__,
                    "message": _jsonable(message),
                }
            )
            if len(events) >= args.max_events:
                break

    try:
        await asyncio.wait_for(collect(), timeout=args.timeout_seconds)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "stage": "query",
            "error": "timeout",
            "events": events,
        }
    except Exception as err:
        return {
            "ok": False,
            "stage": "query",
            "error": str(err),
            "error_type": type(err).__name__,
            "events": events,
        }

    return {
        "ok": bool(events),
        "stage": "query",
        "package": "claude-agent-sdk",
        "module": "claude_agent_sdk",
        "version": version,
        "events": events,
    }


def main() -> int:
    args = build_parser().parse_args()
    result = asyncio.run(run_smoke(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
