# BotsDock Agent Connector

Provider-neutral connector for BotsDock Agent Workbench.

The connector runs on the user's machine and connects outbound to Codex Web.
The user does not choose a provider on the command line. Each saved machine
connection first bootstraps against Codex Web, Codex Web returns that machine's
provider, and the connector starts the matching runtime driver.

Supported providers:

- `codex`: local `codex app-server`
- `claude_code`: official Claude Agent SDK (`claude-agent-sdk`)

## Development

Install the package in editable mode:

```bash
python3 -m pip install -e .
```

Install Claude support when needed:

```bash
python3 -m pip install -e '.[claude]'
```

Run the generated registration command from the workspace you want the agent to
use:

```bash
botsdock-agent-connector --machine-id mach_xxx --token token_xxx
```

`https://www.botsdock.cn` is the default backend. Pass `--server <base_url>`
only for staging, self-hosted, or local debugging environments.

After each successful registration, the connector token is saved in
`.botsdock_agent_connector.json`, and the registration command exits. A plain
connector start supervises every saved machine connection in one process, so
Codex and Claude Code can run side by side:

```bash
botsdock-agent-connector
```

Pass `--machine-id <id>` without `--token` when you want to debug just one saved
machine connection.

Claude Code does not currently expose a Codex-style thread history listing API
through the SDK. The connector returns an empty history page for explicit history
requests; live turns started through BotsDock are still persisted by Codex Web.

## Protocol

The first WebSocket message is provider-neutral:

```json
{"type":"connector.bootstrap"}
```

Codex Web validates the machine token and returns:

```json
{"type":"connector.bootstrap","provider":"codex"}
```

The connector then sends the regular `connector.hello` with provider-specific
capabilities and starts that provider's runtime.
