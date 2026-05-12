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

Claude Code history import is best-effort and isolated inside the
`claude_code` provider driver. The connector first tries the official Claude
Agent SDK session APIs, then falls back to local transcript JSONL files under
Claude Code's project history directory. Imported history is converted to the
same `thread.sync` and `thread.history` shapes used by the rest of BotsDock.
Claude transcript records that only describe local slash commands, such as
`<local-command-caveat>` or `<local-command-stdout>`, are filtered before sync.
Sessions with no real user turn are skipped. History sync lists Claude sessions
across local projects, while JSONL fallback scans only top-level project
transcripts and skips subagent transcript noise.
Live turns started through BotsDock remain the source of truth for new events.

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
