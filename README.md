# BotsDock Connector

Provider-neutral connector for BotsDock Agent Workbench.

The connector runs on the user's machine and connects outbound to Codex Web.
A physical machine is registered once, then the connector reports all provider
runtimes available on that machine over a single WebSocket connection.

## Supported providers

- `codex` — local `codex app-server`
- `claude_code` — Claude Agent SDK (`claude-agent-sdk`)

## Installation

Until the package is published to PyPI, install from GitHub:

```bash
python3 -m pip install --user --upgrade git+https://github.com/bourne015/botsdock-connector.git
```

After installation, make sure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

> **pip too old?** If the install produces an `UNKNOWN-0.0.0` wheel, your pip is
> ≤ 22.0.2. Upgrade pip first:
>
> ```bash
> python3 -m pip uninstall -y UNKNOWN
> python3 -m pip install --user --upgrade "pip>22.0.2"
> python3 -m pip install --user --upgrade git+https://github.com/bourne015/botsdock-connector.git
> ```

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
restart `botsdock-connector` after the upgrade.

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

Codex Web validates the machine token and returns:

```json
{"type":"connector.bootstrap","provider":"agent"}
```

The connector then sends `connector.hello` with `provider=agent` and a
`provider_runtimes` array. Codex Web routes workspace/thread/turn/approval
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
