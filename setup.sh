#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_PATH="${YUANCLAW_TEMPLATE_PATH:-$ROOT_DIR/config/config.json.template}"
YUANCLAW_HOME="${YUANCLAW_HOME:-$HOME/.yuanclaw}"
JSON_PATH="$YUANCLAW_HOME/config.json"

if [[ ! -f "$TEMPLATE_PATH" ]]; then
  echo "[ERROR] Missing template: $TEMPLATE_PATH"
  exit 1
fi

mkdir -p "$YUANCLAW_HOME"

if [[ -f "$JSON_PATH" ]]; then
  echo "[INFO] Found existing config: $JSON_PATH"
  read -r -p "Reuse current config and quit? [Y/n]: " REUSE_EXISTING
  REUSE_EXISTING="${REUSE_EXISTING:-Y}"
  if [[ "$REUSE_EXISTING" =~ ^[Yy]$ ]]; then
    echo "[OK] Keeping existing config."
    echo "[NEXT] Start gateway: python3 -m yuanclaw gateway"
    exit 0
  fi
else
  cp "$TEMPLATE_PATH" "$JSON_PATH"
  chmod 600 "$JSON_PATH"
  echo "[OK] Created $JSON_PATH from template."
fi

choose_provider() {
  echo >&2
  echo "Choose provider:" >&2
  echo "  1) openrouter (Recommended)" >&2
  echo "  2) openai" >&2
  echo "  3) anthropic" >&2
  echo "  4) moonshot" >&2
  echo "  5) deepseek" >&2
  echo "  6) custom" >&2
  read -r -p "Provider [1-6, default 1]: " choice
  case "${choice:-1}" in
    1) PROVIDER="openrouter" ;;
    2) PROVIDER="openai" ;;
    3) PROVIDER="anthropic" ;;
    4) PROVIDER="moonshot" ;;
    5) PROVIDER="deepseek" ;;
    6) PROVIDER="custom" ;;
    *) PROVIDER="openrouter" ;;
  esac
}

default_model_for_provider() {
  case "$1" in
    openrouter) echo "anthropic/claude-opus-4-5" ;;
    openai) echo "gpt-4.1" ;;
    anthropic) echo "claude-opus-4-1" ;;
    moonshot) echo "moonshot-v1-32k" ;;
    deepseek) echo "deepseek-chat" ;;
    custom) echo "custom-model" ;;
    *) echo "anthropic/claude-opus-4-5" ;;
  esac
}

read_secret() {
  local prompt="$1"
  local value=""
  if [[ -t 0 ]]; then
    read -r -s -p "$prompt" value
    echo
  else
    read -r -p "$prompt" value
  fi
  printf "%s" "$value"
}

PROVIDER=""
choose_provider
MODEL_DEFAULT="$(default_model_for_provider "$PROVIDER")"

echo
read -r -p "Model [default: $MODEL_DEFAULT]: " MODEL
MODEL="${MODEL:-$MODEL_DEFAULT}"

echo
API_KEY="$(read_secret "API key for provider '$PROVIDER' (input hidden): ")"

WORKSPACE_DEFAULT="~/.yuanclaw/workspace"
read -r -p "Workspace path [default: $WORKSPACE_DEFAULT]: " WORKSPACE
WORKSPACE="${WORKSPACE:-$WORKSPACE_DEFAULT}"

read -r -p "Temperature [default: 0.1]: " TEMPERATURE
TEMPERATURE="${TEMPERATURE:-0.1}"

read -r -p "Max tokens [default: 8192]: " MAX_TOKENS
MAX_TOKENS="${MAX_TOKENS:-8192}"

read -r -p "Max tool iterations [default: 40]: " MAX_TOOL_ITERATIONS
MAX_TOOL_ITERATIONS="${MAX_TOOL_ITERATIONS:-40}"

read -r -p "Restrict tools to workspace? [Y/n]: " RESTRICT_WORKSPACE
RESTRICT_WORKSPACE="${RESTRICT_WORKSPACE:-Y}"
if [[ "$RESTRICT_WORKSPACE" =~ ^[Nn]$ ]]; then
  RESTRICT_WORKSPACE_BOOL="false"
else
  RESTRICT_WORKSPACE_BOOL="true"
fi

read -r -p "Exec timeout seconds [default: 60]: " EXEC_TIMEOUT
EXEC_TIMEOUT="${EXEC_TIMEOUT:-60}"

read -r -p "Gateway host [default: 0.0.0.0]: " GATEWAY_HOST
GATEWAY_HOST="${GATEWAY_HOST:-0.0.0.0}"

read -r -p "Gateway port [default: 18790]: " GATEWAY_PORT
GATEWAY_PORT="${GATEWAY_PORT:-18790}"

read -r -p "Enable Telegram channel? [y/N]: " ENABLE_TELEGRAM
ENABLE_TELEGRAM="${ENABLE_TELEGRAM:-N}"
if [[ "$ENABLE_TELEGRAM" =~ ^[Yy]$ ]]; then
  TELEGRAM_ENABLED="true"
  TELEGRAM_TOKEN="$(read_secret "Telegram bot token (input hidden): ")"
  read -r -p "Telegram allowFrom list (comma-separated user IDs, optional): " TELEGRAM_ALLOW_FROM_CSV
  read -r -p "Telegram groupPolicy [mention/open, default mention]: " TELEGRAM_GROUP_POLICY
  TELEGRAM_GROUP_POLICY="${TELEGRAM_GROUP_POLICY:-mention}"
else
  TELEGRAM_ENABLED="false"
  TELEGRAM_TOKEN=""
  TELEGRAM_ALLOW_FROM_CSV=""
  TELEGRAM_GROUP_POLICY="mention"
fi

read -r -p "Brave Search API key (optional): " BRAVE_API_KEY

export JSON_PATH PROVIDER MODEL API_KEY WORKSPACE TEMPERATURE MAX_TOKENS MAX_TOOL_ITERATIONS \
  RESTRICT_WORKSPACE_BOOL EXEC_TIMEOUT GATEWAY_HOST GATEWAY_PORT TELEGRAM_ENABLED \
  TELEGRAM_TOKEN TELEGRAM_ALLOW_FROM_CSV TELEGRAM_GROUP_POLICY BRAVE_API_KEY

python3 <<'PY'
import json
import pathlib
import os

json_path = pathlib.Path(os.environ["JSON_PATH"]).expanduser()

try:
    data = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
except json.JSONDecodeError as exc:
    print(f"[ERROR] Invalid JSON in {json_path}: {exc}")
    raise SystemExit(1)
if not isinstance(data, dict):
    print("[ERROR] config.json must be a JSON object at root.")
    raise SystemExit(1)

agents = data.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
defaults["workspace"] = os.environ["WORKSPACE"]
defaults["provider"] = os.environ["PROVIDER"]
defaults["model"] = os.environ["MODEL"]
defaults["temperature"] = float(os.environ["TEMPERATURE"])
defaults["maxTokens"] = int(os.environ["MAX_TOKENS"])
defaults["maxToolIterations"] = int(os.environ["MAX_TOOL_ITERATIONS"])

providers = data.setdefault("providers", {})
provider_name = os.environ["PROVIDER"]
provider_entry = providers.setdefault(provider_name, {})
if not isinstance(provider_entry, dict):
    provider_entry = {}
    providers[provider_name] = provider_entry
provider_entry["apiKey"] = os.environ["API_KEY"]

channels = data.setdefault("channels", {})
telegram = channels.setdefault("telegram", {})
if not isinstance(telegram, dict):
    telegram = {}
    channels["telegram"] = telegram

def csv_to_list(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]

telegram["enabled"] = os.environ["TELEGRAM_ENABLED"].lower() == "true"
telegram["token"] = os.environ["TELEGRAM_TOKEN"]
telegram["allowFrom"] = csv_to_list(os.environ["TELEGRAM_ALLOW_FROM_CSV"])
telegram["groupPolicy"] = os.environ["TELEGRAM_GROUP_POLICY"]

tools = data.setdefault("tools", {})
if not isinstance(tools, dict):
    tools = {}
    data["tools"] = tools
tools["restrictToWorkspace"] = os.environ["RESTRICT_WORKSPACE_BOOL"].lower() == "true"
exec_cfg = tools.setdefault("exec", {})
if not isinstance(exec_cfg, dict):
    exec_cfg = {}
    tools["exec"] = exec_cfg
exec_cfg["timeout"] = int(os.environ["EXEC_TIMEOUT"])

web_cfg = tools.setdefault("web", {})
if not isinstance(web_cfg, dict):
    web_cfg = {}
    tools["web"] = web_cfg
search_cfg = web_cfg.setdefault("search", {})
if not isinstance(search_cfg, dict):
    search_cfg = {}
    web_cfg["search"] = search_cfg
search_cfg["apiKey"] = os.environ["BRAVE_API_KEY"]

gateway = data.setdefault("gateway", {})
if not isinstance(gateway, dict):
    gateway = {}
    data["gateway"] = gateway
gateway["host"] = os.environ["GATEWAY_HOST"]
gateway["port"] = int(os.environ["GATEWAY_PORT"])

json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

chmod 600 "$JSON_PATH"
mkdir -p "$YUANCLAW_HOME/workspace"

echo "[OK] Generated runtime config: $JSON_PATH"
echo "[OK] Workspace ready: $YUANCLAW_HOME/workspace"
echo "[NEXT] Start gateway: python3 -m yuanclaw gateway"
