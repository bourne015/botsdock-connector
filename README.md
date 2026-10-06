# BotsDock Connector

Provider-neutral connector for BotsDock Agent Workbench.

The connector runs on the user's machine and connects outbound to BotsDock Console API.
A physical machine is registered once, then the connector reports all provider
runtimes available on that machine over a single WebSocket connection.

## Supported providers

- `codex` — local `codex app-server`
- `claude_code` — Claude Agent SDK (`claude-agent-sdk`)

## Installation

On macOS and Linux, install into a dedicated virtual environment (Python 3.10+
and Git are required). Download and run the standalone installer:

```bash
installer=$(mktemp)
curl -fsSL https://raw.githubusercontent.com/bourne015/botsdock-connector/main/botsdock_connector/install.py -o "$installer" && python3 "$installer"
rm -f "$installer"
export PATH="$HOME/.botsdock/connector/current/bin:$PATH"
```

The PATH above works regardless of the Python distribution's user-site directory.
Add the same `export PATH="$HOME/.botsdock/connector/current/bin:$PATH"` line to
`~/.zshrc` (zsh) or `~/.bashrc` (bash) so new terminals can find the command.
If migrating from a `pip --user` install, put this directory before the old command
and confirm with `command -v botsdock-connector`. Stop the old daemon before starting
the managed installation; machine registration is retained.

The installer does not modify the system Python or your saved machine credentials.
For development or Windows, use a manually created virtual environment and install
with `python -m pip install git+https://github.com/bourne015/botsdock-connector.git`.

## Upgrade

```bash
botsdock-connector upgrade
```

GitHub upgrades force a reinstall so new commits are picked up even when the
version number hasn't changed. Pin a specific release tag:

```bash
botsdock-connector upgrade --version v0.1.5
```

After publishing to PyPI, switch the source:

```bash
botsdock-connector upgrade --source pypi
```

For private mirrors, set `BOTSDOCK_CONNECTOR_UPGRADE_SPEC` or pass
`--package-spec`. The running connector process is not hot-swapped — stop and
restart `botsdock-connector` after the upgrade. In background mode, run
`botsdock-connector stop`, then repeat your original `start` command, preserving
custom server, machine, workspace and runtime options. In foreground mode use
Ctrl-C, then repeat the original command. Upgrading never interrupts active turns.

## Quick start

### Register

```bash
botsdock-connector --machine-id mach_xxx --token token_xxx
```

`https://www.botsdock.cn` is the default backend. Pass `--server <base_url>`
only for staging, self-hosted, or local debugging.

After a successful registration the connector token is saved to
`~/.botsdock_connector.json` and the command exits.

### Run

```bash
botsdock-connector
```

This starts every saved machine connection in one process. For a normal
physical machine there is one connection — Codex and Claude Code run side by
side through it.

Pass `--machine-id <id>` (without `--token`) to debug a single saved connection.

## Configuration

### Runtime profiles

Multiple profiles let you switch Claude Code configurations (e.g. DeepSeek
gateway, corporate proxy) without re-registering the machine. The default
profile id is `default`.

Register with a non-default profile:

```bash
botsdock-connector \
  --machine-id mach_xxx \
  --token token_xxx \
  --runtime-profile deepseek \
  --runtime-profile-name "DeepSeek" \
  --env-file ~/.botsdock/botsdock_connector.deepseek.env
```

Profiles are stored in `~/.botsdock_connector.json`. On startup the connector
reports non-sensitive metadata — profile id, display name, env key names,
model, and CLI label — via `connector.hello`. Env file contents and tokens are
never sent to BotsDock.

### Environment file

Store provider credentials locally so they reach the SDK child process without
being sent to the server:

```bash
mkdir -p ~/.botsdock
cat > ~/.botsdock/botsdock_connector.env <<'EOF'
ANTHROPIC_BASE_URL=https://your-gateway.example/anthropic
ANTHROPIC_AUTH_TOKEN=your-local-token
ANTHROPIC_MODEL=your-model-name
EOF
botsdock-connector
```

For DeepSeek and other Anthropic-compatible gateways, if only
`ANTHROPIC_AUTH_TOKEN` is set the connector mirrors it as `ANTHROPIC_API_KEY` in
the SDK child process. This avoids a false Claude App/Keychain login prompt
during session resume.

On macOS with zsh, `~/.zprofile` exports are only loaded for login shells.
Either `source ~/.zprofile` before starting the connector, move exports to
`~/.zshrc`, or use the env file above.

### Claude Code setup

The connector runs Claude Code through the local CLI configuration. By default
the Claude Agent SDK chooses its bundled CLI. Set `BOTSDOCK_CLAUDE_BIN`,
`CLAUDE_CODE_BIN`, or pass `--claude-bin <path>` for a custom binary.

The connector preserves `HOME`, `PATH`, and XDG config/cache/data paths. It
loads user, project, and local Claude settings and forwards Anthropic/Claude
Code environment variables, so third-party gateways configured through
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, etc. are visible to the SDK
child process.

Claude Code ignores generic remote turn `model` values (those are often
Codex-specific). Configure models locally with `--model`, `ANTHROPIC_MODEL`,
or a runtime profile / env file.

If you use `claude login` instead of API keys, start the connector as the
same OS user. Do not use `sudo` unless you also point `CLAUDE_CONFIG_DIR` or
`HOME` at the intended user's configuration.

### Legacy token files

Token files from older versions (`.botsdock_agent_connector.json`,
`.codex_connector.json`, `.botsdock_codex_connector.json`) are still read.
New registrations always write to `~/.botsdock_connector.json`.

## Development

```bash
python3 -m pip install -e .
```

## Claude Code history import

History import is best-effort and isolated inside the `claude_code` provider
driver. The connector tries the official Claude Agent SDK session APIs first,
then falls back to local JSONL transcript files under Claude Code's project
history directory.

Imported history is converted to the same `thread.sync` and `thread.history`
shapes used by the rest of BotsDock. Transcript records that only describe
local slash-commands (e.g. `<local-command-caveat>`, `<local-command-stdout>`)
are filtered. Sessions with no real user turn are skipped.

Live turns started through BotsDock remain the source of truth. When a history
snapshot is non-empty it is authoritative for that machine, so stale workspaces
can be pruned by the backend.

## Protocol

The first WebSocket message is provider-neutral:

```json
{"type":"connector.bootstrap"}
```

BotsDock Console API validates the machine token and returns:

```json
{"type":"connector.bootstrap","provider":"agent"}
```

The connector then sends `connector.hello` with `provider=agent` and a
`provider_runtimes` array. BotsDock Console API routes workspace/thread/turn/approval
requests by the provider on each resource:

```json
{
  "type": "connector.hello",
  "provider": "agent",
  "provider_runtimes": [
    {"provider": "codex", "runtime": "codex_app_server"},
    {"provider": "claude_code", "runtime": "claude_agent_sdk"}
  ]
}
```

## Protocol and runtime compatibility

The connector wire protocol is `0.1`. Unknown versions returned by the server are
rejected; legacy 0.1 servers that omit the response version remain supported.

Before making Codex available, the installed binary exports its JSON Schema.
Core thread, turn, steering, history, and model methods must exist. The generated
parameter schemas validate outgoing requests, including `expectedTurnId` and
`itemsView: full`. Stable API is preferred;
experimental API is enabled only when the installed version needs it for a core
method. Repair or upgrade Codex if schema export fails or methods are missing.
This check does not replace an end-to-end smoke after a CLI upgrade. Models and
supported reasoning efforts are read from the runtime catalog, not a fixed list.
Console auto-review maps to `on-request` plus reviewer `auto_review`; full access
maps to `danger-full-access` plus approval policy `never`.

Claude uses local user/project/local permission rules. Locally allowed tools can
skip web approval. Codex sandbox values are rejected for Claude; supported approval
policies are `on-request` and `never` (`dontAsk`, which denies required prompts).
Stale or completed approvals are rejected instead of acknowledged as resolved.
Claude SDK 0.2.163 or later is required (below 0.3). Partial messages are enabled;
tool results are read from SDK user messages. `AskUserQuestion` uses a dedicated
question form on the existing pending-request channel and returns the original
questions plus answers to the SDK. It is not a permission grant.
Codex user-input and MCP elicitation requests are currently rejected explicitly;
unsupported records preserve their questions and do not block the web console.

Run local tests with `python -m pytest`. These tests use simulated runtime events;
run `botsdock_connector/claude_agent_sdk_smoke.py` with local authentication for
real SDK verification. Never commit credentials or sensitive smoke output.
