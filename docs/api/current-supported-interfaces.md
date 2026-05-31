# Current Supported Interfaces

This document summarizes the external interfaces currently implemented in code for YuanClaw-core.

Scope:
- Local Studio HTTP API and WebSocket API
- CLI commands
- Runtime chat channel integrations
- LLM provider integrations
- Local WhatsApp bridge protocol

Source baseline:
- Branch: `dev`
- Generated from current code after rebasing onto `origin/dev`

## 1. Studio API

Entry points:
- Python API server: `yuanclaw.api.server`
- CLI start command: `yuanclaw serve`

Server characteristics:
- Framework: FastAPI
- Default bind: `127.0.0.1`
- Default port: `18789`
- CORS allowlist: `http://localhost:5174`, `http://127.0.0.1:5174`, `tauri://localhost`

### 1.1 HTTP Endpoints

#### `GET /health`

Purpose:
- Basic process health probe

Response fields:
- `ok`
- `running`
- `pid`
- `version`

#### `GET /api/status`

Purpose:
- Dashboard/runtime summary

Response includes:
- `running`
- `host`
- `port`
- `pid`
- `version`
- `model`
- `workspace`
- `started_at`
- `uptime_sec`
- `sessions_count`
- `skills_count`
- `channels`
- `cron`

#### `GET /api/skills`

Purpose:
- List built-in skills discovered from the packaged `yuanclaw/skills` directory

Response shape:
- `items`
- `total`

Each item includes:
- `id`
- `name`
- `description`
- `version`
- `enabled`

#### `GET /api/channels`

Purpose:
- List configured runtime channels and their status

Response shape:
- `items`
- `total`

Each item includes:
- `id`
- `name`
- `enabled`
- `configured`
- `running`

#### `GET /api/cron/jobs`

Purpose:
- List cron jobs known to the runtime

Response shape:
- `items`
- `summary`

`summary` includes:
- `total`
- `active`
- `paused`
- `failed`

#### `GET /api/sessions`

Purpose:
- List persisted sessions

Response shape:
- `items`
- `total`

Each session item includes:
- `key`
- `created_at`
- `updated_at`
- `path`
- `message_count`
- `last_role`
- `last_message_preview`

#### `GET /api/sessions/{session_key:path}`

Purpose:
- Replay one persisted session and its full message list

Path behavior:
- `session_key` is URL-decoded before lookup
- Empty key returns HTTP 400
- Missing sessions return HTTP 404 and are not created as a side effect

Response includes:
- `key`
- `created_at`
- `updated_at`
- `last_consolidated`
- `metadata`
- `messages`

Media replay behavior:
- Local `media` filesystem paths are removed from each message
- Servable media under YuanClaw's media directory is exposed as `media_urls`

#### `GET /api/sessions/{session_key:path}/messages`

Purpose:
- Replay one persisted session using the same read-only semantics as `GET /api/sessions/{session_key:path}`

Response includes:
- `key`
- `created_at`
- `updated_at`
- `last_consolidated`
- `metadata`
- `messages`

Media replay behavior:
- Local `media` filesystem paths are removed from each message
- Servable media under YuanClaw's media directory is exposed as signed `media_urls`

#### `GET /api/media/{sig}/{payload}`

Purpose:
- Serve signed media artifacts referenced by session replay

Behavior:
- Only signed files under YuanClaw's media directory are served
- Invalid signatures return HTTP 401
- Missing or out-of-scope files return HTTP 404
- Gateway token auth applies when configured
- MIME types use an allowlist; unknown types fall back to `application/octet-stream`
- Responses include `X-Content-Type-Options: nosniff`
- SVG responses include a sandboxing CSP
- Single byte ranges are supported with HTTP 206
- Unsatisfiable ranges return HTTP 416 with `Content-Range: bytes */size`

#### `GET /api/config`

Purpose:
- Read active config for Studio settings UI

Response includes:
- `model`
- `workspace`
- `providers`
- `raw`

`providers` entries include:
- `id`
- `name`
- `configured`
- `is_default`
- `api_key_masked`
- `api_base`

#### `PUT /api/config`

Purpose:
- Replace persisted runtime config

Request body:
- Full config payload validated by `Config.model_validate(...)`

Response includes:
- `ok`
- `requires_restart`
- `saved_at`

Failure mode:
- Invalid payload returns HTTP 400

### 1.2 WebSocket Endpoints

#### `WS /ws/chat`

Purpose:
- Interactive Studio chat transport

Server -> client message types:
- `ready`
- `pong`
- `goal_status`
- `progress`
- `done`
- `turn_end`
- `error`

Client -> server message types:
- `ping`
- `chat`

`chat` payload fields:
- `type`
- `content`
- `sessionKey`
- `skillNames`
- `cliApps`
- `mcpPresets`
- `workspaceScope` / `workspace_scope`

Behavior:
- Missing `content` returns an `error` event
- Default session key is `studio:default`
- `cliApps` and `mcpPresets` are forwarded as agent runtime attachment metadata
- `workspaceScope` / `workspace_scope` is forwarded as `workspace_scope` agent metadata and can override the current turn project root/access mode
- Turn start emits `goal_status` with `status=running`
- Progress callbacks stream intermediate agent output
- Progress frames may include `toolHint`, `toolEvents`, and `fileEditEvents`
- `apply_patch` emits file edit start/end/error events through `fileEditEvents`
- Final completion emits `done` with bounded `goalState`
- Turn completion emits `turn_end` with bounded `goalState`

#### `WS /ws/events`

Purpose:
- Runtime event stream for Studio UI

Initial message:
- `ready`

Observed event types emitted by runtime:
- `core.started`
- `core.stopped`
- `bus.inbound`
- `studio.message_received`
- `channel.message_received`
- `agent.progress`
- `agent.tool_hint`
- `agent.goal_status`
- `agent.reply_done`
- `agent.turn_end`
- `channel.reply_sent`

### Agent Compaction

Configuration:
- `agents.defaults.maxConcurrentSubagents`
- `agents.defaults.compaction.idleCompactAfterMinutes`
- `agents.defaults.compaction.sessionTtlMinutes`

Behavior:
- Subagent concurrency defaults to `1`; when the limit is reached, `spawn` returns a bounded rejection message instead of starting another background task
- Default is `0`, which disables idle auto-compact
- When enabled, idle persisted sessions past the TTL are compacted in the background
- Compaction keeps the recent message suffix, writes memory/history updates, and stores `_last_summary` in session metadata
- The next turn can receive that summary in runtime context as previous conversation summary without persisting it into the user transcript

### Runner Recovery

Runtime behavior:
- Blank final LLM responses are retried once with an explicit non-empty response prompt
- `finish_reason=length` responses are followed by a continuation prompt and concatenated with the next response

## 2. CLI Interfaces

Python entrypoint:
- Console script: `yuanclaw = yuanclaw.cli.commands:app`

### Top-level commands

#### `yuanclaw onboard`
- Initialize config and workspace
- Create or refresh `~/.yuanclaw/config.json`
- Sync workspace templates

#### `yuanclaw serve`
- Start the local Studio API server

Main options:
- `--host`
- `--port`, `-p`
- `--workspace`, `-w`
- `--config`, `-c`
- `--with-channels`

#### `yuanclaw gateway`
- Start the agent loop plus channel manager, cron service, and heartbeat service

Main options:
- `--port`, `-p`
- `--workspace`, `-w`
- `--verbose`, `-v`
- `--config`, `-c`

#### `yuanclaw agent`
- Run one-shot direct chat or interactive terminal chat

Main options:
- `--message`, `-m`
- `--session`, `-s`
- `--workspace`, `-w`
- `--config`, `-c`
- `--markdown/--no-markdown`
- `--logs/--no-logs`

Behavior:
- With `--message`, runs one direct request
- Without `--message`, starts interactive terminal mode

#### `yuanclaw status`
- Print config, workspace, model, and provider status

### Channel subcommands

#### `yuanclaw channels status`
- Print enabled/configured status for channel integrations

#### `yuanclaw channels login`
- Start the local WhatsApp bridge and display QR login flow

#### `yuanclaw channels pairings`
- List pending channel pairing codes

#### `yuanclaw channels approve-pairing CODE`
- Approve a pending pairing code

#### `yuanclaw channels deny-pairing CODE`
- Deny and remove a pending pairing code

#### `yuanclaw channels revoke-pairing CHANNEL SENDER_ID`
- Revoke an approved channel sender

### Provider subcommands

#### `yuanclaw provider login openai-codex`
- Interactive OAuth login for OpenAI Codex

#### `yuanclaw provider login github-copilot`
- Device/OAuth bootstrap for GitHub Copilot

## 3. Runtime Channel Integrations

The runtime can initialize the following channels when enabled in config:
- Telegram
- WhatsApp
- Discord
- Feishu / Lark
- Mochat
- DingTalk
- Email
- Slack
- QQ
- Matrix
- Signal

Channel manager responsibilities:
- Start enabled channels
- Stop enabled channels
- Dispatch outbound messages to the target channel
- Enforce `allow_from` validation for configured channels

Notes:
- Studio API status endpoints expose these channels as runtime-facing integration surfaces
- Actual availability at runtime depends on config, credentials, and optional dependencies

### Signal

Configuration fields:
- `channels.signal.phoneNumber`
- `channels.signal.daemonHost`
- `channels.signal.daemonPort`
- `channels.signal.attachmentsDir`
- `channels.signal.healthCheckEnabled`
- `channels.signal.receiveEvents`
- `channels.signal.reconnectDelayS`
- `channels.signal.maxReconnectDelayS`
- `channels.signal.dm`
- `channels.signal.group`

Runtime behavior:
- Uses the signal-cli daemon JSON-RPC `/api/v1/rpc` endpoint for outbound `send`
- Optional startup health check calls `/api/v1/check`
- Optional receive loop consumes `/api/v1/events` SSE envelopes and reconnects with backoff after transient errors
- DM/group allowlist and pairing approval gate inbound messages before publishing them to the bus
- When `channels.signal.dm.pairingReplyEnabled=true`, denied DMs receive a pairing code reply; the default is false
- Pairing approvals can be managed with `yuanclaw channels pairings`, `approve-pairing`, `deny-pairing`, and `revoke-pairing`
- Inbound attachment paths are constrained to `attachmentsDir`
- Outbound markdown is currently downgraded to a small plain-text subset

## 4. LLM Provider Integrations

Registered providers:
- `custom`
- `azure_openai`
- `openrouter`
- `aihubmix`
- `siliconflow`
- `volcengine`
- `anthropic`
- `openai`
- `openai_codex`
- `github_copilot`
- `deepseek`
- `gemini`
- `zhipu`
- `dashscope`
- `moonshot`
- `minimax`
- `vllm`
- `groq`
- `bedrock`
- `novita`
- `longcat`
- `stepfun`
- `stepplan`

Provider categories in current code:
- Direct providers: `custom`, `azure_openai`
- Native Anthropic provider: `anthropic` through Anthropic Messages API
- Native OpenAI-compatible providers: `openrouter`, `volcengine`, `byteplus`, `novita`, `longcat`, `stepfun`, `xiaomi_mimo`, `ant_ling` and other registry entries with OpenAI chat-completions-compatible bases
- Native Bedrock provider: `bedrock` through AWS Bedrock Runtime Converse APIs
- OAuth providers: `openai_codex`, `github_copilot`
- Gateway providers: `openrouter`, `aihubmix`, `siliconflow`, `volcengine`
- Standard providers: `openai`, `deepseek`, `gemini`, `zhipu`, `dashscope`, `moonshot`, `minimax`, `groq`, `novita`, `longcat`, `stepfun`, `stepplan`
- Local provider: `vllm`

Provider factory behavior:
- `fallbackModels` wraps the primary provider in a fallback chain
- Provider config accepts upstream-style `extraBody`, `apiType`, `region`, and `profile` fields
- Anthropic bypasses `LiteLLMProvider`, normalizes `/v1` API bases for the Anthropic SDK, supports extra headers, prompt cache markers, tool calls, tool results, thinking blocks, stream deltas, and cached token usage
- Native OpenAI-compatible providers bypass `LiteLLMProvider` and send `extraBody` through the OpenAI SDK `extra_body` request field
- Bedrock bypasses `LiteLLMProvider`, uses `region` / `profile` / `apiBase`, and accepts provider-specific `extraBody` as Bedrock `additionalModelRequestFields`
- Bedrock can match from an explicit `bedrock/...` model prefix or from Bedrock `region` / `profile` config without an API key
- Some non-OpenAI-compatible or special-protocol providers still route through `LiteLLMProvider`; remaining native migration is tracked in the v0.2.0 feature sync design doc

## 5. Agent Core Runtime Attachments and Tools

### CLI Apps

Runtime attachment fields:
- `cliApps`
- `cli_apps`

Behavior:
- Attachments are normalized and persisted into session metadata
- Runtime context includes CLI app attachment lines
- `run_cli_app` is registered only when `tools.cliApps.enabled=true`
- `run_cli_app` reads installed apps from workspace `apps/cli/installed.json`
- App execution uses argv subprocesses, not shell strings
- `working_dir` is constrained by workspace policy
- Python core service `CliAppService` manages local `apps/cli/catalog.json` and `apps/cli/installed.json`
- `CliAppService` supports replace/list catalog, install, settings update, uninstall, and installed entry point test
- Catalog listing merges installed state and installed settings

Current boundary:
- CLI Apps catalog/install/update/uninstall/settings exists as a Python core service, not as a public HTTP API
- Remote registry refresh is not yet implemented
- Remote install command execution is not yet implemented

### MCP Presets

Runtime attachment fields:
- `mcpPresets`
- `mcp_presets`

Behavior:
- Attachments are normalized and persisted into session metadata
- Runtime context includes MCP preset attachment lines
- Python core service `McpPresetService` manages preset settings in `Config.tools.mcp_servers`
- `McpPresetService` supports builtin preset catalog payloads, enable, remove, dependency checks, and MCP connection tests
- Preset enable writes `MCPServerConfig` entries and validates required settings without leaking secret values in payloads
- Stdio presets can use managed runtime working directories through `MCPServerConfig.cwd`
- Runtime context helpers can distinguish stale settings, configured-but-not-live, and connected MCP preset state when configured/connected server names are provided

Current boundary:
- MCP presets settings exists as a Python core service, not as a public HTTP API
- Only the Agent Core builtin preset subset is currently mirrored
- OAuth preset UX and full WebUI import/custom flows are not yet implemented

### Core Tools

New runtime tools:
- `apply_patch`
- `write_stdin`
- `list_exec_sessions`
- `long_task`
- `complete_goal`
- `generate_image`
- `run_cli_app`
- `spawn`

Security behavior:
- File and media operations are checked by workspace policy
- Long exec sessions are scoped to the owning `AgentLoop` and current tool context `channel:chat_id`
- Shell execution only forwards an explicit safe environment allowlist
- Image reference paths must be inside the workspace or YuanClaw media directory
- Subagent `spawn` inherits the current turn workspace scope before running subagent tools
- Subagent LLM calls use the same per-session runner wall-timeout policy as normal turns; active sustained goals disable that wall timeout
- Subagent startup is capped by `agents.defaults.maxConcurrentSubagents`

## 6. Local WhatsApp Bridge Protocol

Node entrypoint:
- `bridge/src/index.ts`

Transport:
- Local WebSocket server
- Default address: `ws://127.0.0.1:3001`
- Optional shared-token auth via `BRIDGE_TOKEN`

### Client -> bridge messages

#### Auth handshake
```json
{"type":"auth","token":"<bridge-token>"}
```

#### Send message
```json
{"type":"send","to":"<chat-id>","text":"<content>"}
```

### Bridge -> client messages

#### Outbound acknowledgement
```json
{"type":"sent","to":"<chat-id>"}
```

#### Incoming WhatsApp message
- `type: "message"`
- Fields may include:
  - `id`
  - `sender`
  - `pn`
  - `content`
  - `timestamp`
  - `isGroup`
  - `media`

#### QR event
- `type: "qr"`
- Includes `qr`

#### Status event
- `type: "status"`
- Includes `status`

#### Error event
- `type: "error"`

Bridge-side WhatsApp handling currently supports inbound extraction for:
- text
- extended text
- image caption
- video caption
- document caption
- voice/audio placeholder
- downloaded image/document/video media attachments

## 6. Code References

Primary sources:
- `yuanclaw/api/server.py`
- `yuanclaw/cli/commands.py`
- `yuanclaw/channels/manager.py`
- `yuanclaw/config/schema.py`
- `yuanclaw/providers/registry.py`
- `bridge/src/index.ts`
- `bridge/src/server.ts`
- `bridge/src/whatsapp.ts`
- `yuanclaw/session/manager.py`
- `tests/test_studio_api.py`
