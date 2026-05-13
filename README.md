# BotsDock Connector

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

Run the generated registration command once from the directory where you want
to keep the connector token file:

```bash
botsdock-connector --machine-id mach_xxx --token token_xxx
```

`https://www.botsdock.cn` is the default backend. Pass `--server <base_url>`
only for staging, self-hosted, or local debugging environments.

After each successful registration, the connector token is saved in
`.botsdock_connector.json`, and the registration command exits. A plain
connector start supervises every saved machine connection in one process, so
Codex and Claude Code can run side by side:

```bash
botsdock-connector
```

Pass `--machine-id <id>` without `--token` when you want to debug just one saved
machine connection.

For Claude Code, the connector start directory is not treated as a project.
Historical projects come from Claude Code's own session index, and new turns
should receive an explicit workspace `cwd` from BotsDock. `--cwd <path>` is only
an optional fallback default for debugging or one-off local setups.

Claude Code runs through the local CLI configuration available to the connector
process. By default the Claude Agent SDK chooses its bundled CLI. Set
`BOTSDOCK_CLAUDE_BIN`, `CLAUDE_CODE_BIN`, or pass `--claude-bin <path>` only if
you need a specific SDK-compatible CLI binary or wrapper. The connector loads
user, project, and local Claude settings and forwards Anthropic/Claude Code
environment variables, so third-party API gateways configured through
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, or related model variables are
visible to the SDK child process.

If the connector is started from a shell that does not already export those
variables, keep them in a local-only env file instead:

```bash
mkdir -p ~/.botsdock
cat > ~/.botsdock/botsdock_connector.env <<'EOF'
ANTHROPIC_BASE_URL=https://your-gateway.example/anthropic
ANTHROPIC_AUTH_TOKEN=your-local-token
ANTHROPIC_MODEL=your-model-name
EOF
botsdock-connector
```

On macOS with zsh, variables in `~/.zprofile` are only loaded for login shells.
If `botsdock-connector` logs `env_keys` without `ANTHROPIC_BASE_URL` and
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`, either run `source ~/.zprofile`
before starting the connector, move those exports to `~/.zshrc`, or use the
dedicated `~/.botsdock/botsdock_connector.env` file above.

The env file is read only by the local connector process and is never sent to
BotsDock. You can also point at another file with `BOTSDOCK_CONNECTOR_ENV_FILE` or
`--env-file`.

启动 Claude Code provider 时，connector 会在本地日志里打印 runtime profile
摘要，只包含 profile id、CLI 标签、模型名、env key 名称和 env 文件是否配置，
不会打印任何 token 值。对于 DeepSeek 这类 Anthropic-compatible 网关，如果
本地只设置了 `ANTHROPIC_AUTH_TOKEN`，connector 会仅在 SDK 子进程环境里把它
镜像为 `ANTHROPIC_API_KEY`，避免 Claude Agent SDK 的会话恢复流程误判为需要
Claude App/Keychain 登录。

## Runtime profiles

同一台物理机器可以注册多个 Claude Code machine，并让它们使用不同的本地
CLI/env/model 配置。BotsDock 不保存 Claude、Anthropic 或第三方网关的登录
凭据；这些凭据始终留在用户本机，由 connector 在启动对应 provider runtime
时注入给 Claude Agent SDK。

默认 profile id 是 `default`。如果需要区分 Claude 官方 CLI 登录、DeepSeek
网关、公司代理等本地身份，可以在首次注册对应 machine 时指定 profile：

```bash
botsdock-connector \
  --machine-id mach_xxx \
  --token token_xxx \
  --runtime-profile deepseek \
  --runtime-profile-name "DeepSeek" \
  --env-file ~/.botsdock/botsdock_connector.deepseek.env
```

注册成功后，profile id/name、env 文件路径、模型覆盖和 CLI 路径会写入本地
`.botsdock_connector.json`。之后直接运行：

```bash
botsdock-connector
```

connector 会在一个进程中并发维护所有 saved machine，并为每条连接加载各自
的 runtime profile。`connector.hello` 只会上报 profile 的非敏感元数据，例如
profile id、display name、env key 名称、模型名和 CLI 标签；env 文件内容和
token 值不会发送到 BotsDock。

旧版本生成的 `.botsdock_agent_connector.json`、`.codex_connector.json` 和
`.botsdock_codex_connector.json` 仍会被读取；新的注册和 token 刷新会写入
`.botsdock_connector.json`。

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
When a Claude history snapshot is non-empty, it is authoritative for that
machine, so stale Claude workspaces from older connector behavior can be pruned
by the backend.

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
