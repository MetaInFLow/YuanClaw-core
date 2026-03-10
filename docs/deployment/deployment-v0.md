# YuanClaw Deployment Steps (V0)

## Goal
Provide a clean, first-time YuanClaw setup flow with no nanobot dependency.

## Files involved
- Setup script: `setup.sh`
- Template: `config/config.json.template`
- Runtime config:
  - `~/.yuanclaw/config.json` (editable + runtime file)

## What must be configured first
1. Provider credentials
   - Fill at least one API key under `providers.*.apiKey`
   - Keep `agents.defaults.provider` aligned with the provider key you filled
2. Model
   - Set `agents.defaults.model` to a model available under your provider
3. Telegram (if using Telegram)
   - `channels.telegram.enabled: true`
   - `channels.telegram.token: <your-bot-token>`
   - Optional allowlist: `channels.telegram.allowFrom: [<user_id_1>, <user_id_2>]`
4. Workspace and safety
   - `agents.defaults.workspace` (default: `~/.yuanclaw/workspace`)
   - `tools.restrictToWorkspace` (recommended: `true`)

## setup.sh behavior
`setup.sh` does the following:
1. Creates `~/.yuanclaw/` if missing
2. Copies `config/config.json.template` to `~/.yuanclaw/config.json` on first run
3. Runs an interactive setup wizard and writes your answers into `config.json`
4. Creates `~/.yuanclaw/workspace`

## setup.sh interactive prompts
`setup.sh` asks you to choose or input:
1. Provider (menu: openrouter/openai/anthropic/moonshot/deepseek/custom)
2. Model (supports default suggestion)
3. Provider API key (hidden input)
4. Workspace path
5. Temperature
6. Max tokens
7. Max tool iterations
8. Whether to restrict tools to workspace
9. Exec timeout
10. Gateway host and port
11. Whether to enable Telegram
12. Telegram token + allowFrom + groupPolicy (when enabled)
13. Brave Search API key (optional)

## Standard setup flow
1. Enter repo root:
   - `cd /Users/anthonyf/projects/metainflow/YuanClaw`
2. Run setup:
   - `chmod +x setup.sh`
   - `bash setup.sh`
3. Start gateway:
   - `python3 -m yuanclaw gateway`

## Verification commands
1. Check runtime config exists:
   - `test -f ~/.yuanclaw/config.json && echo ok`
2. Check CLI works:
   - `python3 -m yuanclaw --help`
3. Check Telegram settings were applied:
   - `python3 - <<'PY'\nimport json, pathlib\np=pathlib.Path.home()/'.yuanclaw/config.json'\nd=json.loads(p.read_text())\nprint(d.get('channels',{}).get('telegram',{}))\nPY`
