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
- Get one session and its full message list

Path behavior:
- `session_key` is URL-decoded before lookup
- Empty key returns HTTP 400

Response includes:
- `key`
- `created_at`
- `updated_at`
- `last_consolidated`
- `messages`

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
- `progress`
- `done`
- `error`

Client -> server message types:
- `ping`
- `chat`

`chat` payload fields:
- `type`
- `content`
- `sessionKey`

Behavior:
- Missing `content` returns an `error` event
- Default session key is `studio:default`
- Progress callbacks stream intermediate agent output
- Final completion emits `done`

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
- `agent.reply_done`
- `channel.reply_sent`

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

Channel manager responsibilities:
- Start enabled channels
- Stop enabled channels
- Dispatch outbound messages to the target channel
- Enforce `allow_from` validation for configured channels

Notes:
- Studio API status endpoints expose these channels as runtime-facing integration surfaces
- Actual availability at runtime depends on config, credentials, and optional dependencies

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

Provider categories in current code:
- Direct providers: `custom`, `azure_openai`
- OAuth providers: `openai_codex`, `github_copilot`
- Gateway providers: `openrouter`, `aihubmix`, `siliconflow`, `volcengine`
- Standard providers: `anthropic`, `openai`, `deepseek`, `gemini`, `zhipu`, `dashscope`, `moonshot`, `minimax`, `groq`
- Local provider: `vllm`

## 5. Local WhatsApp Bridge Protocol

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
