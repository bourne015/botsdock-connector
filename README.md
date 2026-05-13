# BotsDock Connector

Provider-neutral connector for BotsDock Agent Workbench.

The connector runs on the user's machine and connects outbound to Codex Web.
The user does not choose a provider on the command line. A physical machine is
registered once, then the connector reports the provider runtimes available on
that machine over the same WebSocket connection.

Supported providers:

- `codex`: local `codex app-server`
- `claude_code`: official Claude Agent SDK (`claude-agent-sdk`)

## Install and upgrade

Until the package is published to PyPI, install from the GitHub repo:

```bash
python3 -m pip install --upgrade git+https://github.com/bourne015/botsdock-connector.git
```

If pip builds an `UNKNOWN-0.0.0` package, upgrade the local packaging tools and
reinstall:

```bash
python3 -m pip uninstall -y UNKNOWN
python3 -m pip install --user --upgrade pip setuptools wheel
python3 -m pip install --user --upgrade --force-reinstall git+https://github.com/bourne015/botsdock-connector.git
```

After installation, users can upgrade in place with:

```bash
botsdock-connector upgrade
```

`upgrade` uses the current Python environment and runs pip against the GitHub
repo by default. A specific release tag can be selected with:

```bash
botsdock-connector upgrade --version v0.1.1
```

After a PyPI release, users can switch the source explicitly:

```bash
botsdock-connector upgrade --source pypi
```

For private mirrors or a custom release channel, set
`BOTSDOCK_CONNECTOR_UPGRADE_SPEC` or pass `--package-spec`. The running
connector process is not hot-swapped; stop and restart `botsdock-connector`
after the upgrade completes.

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
connector start runs every saved machine connection in one process. For a
normal physical machine there is one saved connection, and Codex plus Claude
Code run side by side through that connection:

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

同一台物理机器只需要注册一次。Claude Code 的 CLI/env/model 配置属于这台
机器上的 provider runtime profile。BotsDock 不保存 Claude、Anthropic 或
第三方网关的登录凭据；这些凭据始终留在用户本机，由 connector 在启动
Claude Agent SDK runtime 时注入。

默认 profile id 是 `default`。如果需要使用 DeepSeek 网关、公司代理等本地
身份，可以在首次注册或后续运行 connector 时指定 profile：

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

connector 会在一个进程中维护所有 saved machine。每条 machine connection
会加载本地 runtime profile，并通过 `connector.hello` 上报非敏感元数据，例如
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
{"type":"connector.bootstrap","provider":"agent"}
```

The connector then sends `connector.hello` with `provider=agent` and a
`provider_runtimes` array. Codex Web routes workspace/thread/turn/approval
requests by the provider on each resource.

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
