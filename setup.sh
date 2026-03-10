#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_PATH="${YUANCLAW_TEMPLATE_PATH:-$ROOT_DIR/config/config.yaml.template}"
YUANCLAW_HOME="${YUANCLAW_HOME:-$HOME/.yuanclaw}"
YAML_PATH="$YUANCLAW_HOME/config.yaml"
JSON_PATH="$YUANCLAW_HOME/config.json"

if [[ ! -f "$TEMPLATE_PATH" ]]; then
  echo "[ERROR] Missing template: $TEMPLATE_PATH"
  exit 1
fi

mkdir -p "$YUANCLAW_HOME"

if [[ ! -f "$YAML_PATH" ]]; then
  cp "$TEMPLATE_PATH" "$YAML_PATH"
  chmod 600 "$YAML_PATH"
  echo "[OK] Created $YAML_PATH from template."
  echo "[NEXT] Edit provider API key and Telegram settings in $YAML_PATH."
fi

python3 - "$YAML_PATH" "$JSON_PATH" <<'PY'
import json
import pathlib
import sys

try:
    import yaml
except Exception:
    print("[ERROR] Missing dependency: pyyaml")
    print("[HINT] Install with: python3 -m pip install --user pyyaml")
    raise SystemExit(1)


yaml_path = pathlib.Path(sys.argv[1]).expanduser()
json_path = pathlib.Path(sys.argv[2]).expanduser()

raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
if not isinstance(raw, dict):
    print("[ERROR] config.yaml must be a mapping/object at root.")
    raise SystemExit(1)

data = raw

agents = data.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
defaults.setdefault("workspace", "~/.yuanclaw/workspace")
defaults.setdefault("provider", "openrouter")
defaults.setdefault("model", "anthropic/claude-opus-4-5")
defaults.setdefault("temperature", 0.1)
defaults.setdefault("maxTokens", 8192)
defaults.setdefault("maxToolIterations", 40)

providers = data.setdefault("providers", {})
for name in ("openrouter", "openai", "anthropic"):
    entry = providers.setdefault(name, {})
    if isinstance(entry, dict):
        entry.setdefault("apiKey", "")

channels = data.setdefault("channels", {})
telegram = channels.setdefault("telegram", {})
if isinstance(telegram, dict):
    telegram.setdefault("enabled", False)
    telegram.setdefault("token", "")
    telegram.setdefault("allowFrom", [])
    telegram.setdefault("groupPolicy", "mention")

tools = data.setdefault("tools", {})
if isinstance(tools, dict):
    tools.setdefault("restrictToWorkspace", True)
    exec_cfg = tools.setdefault("exec", {})
    if isinstance(exec_cfg, dict):
        exec_cfg.setdefault("timeout", 60)
    web_cfg = tools.setdefault("web", {})
    if isinstance(web_cfg, dict):
        search_cfg = web_cfg.setdefault("search", {})
        if isinstance(search_cfg, dict):
            search_cfg.setdefault("apiKey", "")

gateway = data.setdefault("gateway", {})
if isinstance(gateway, dict):
    gateway.setdefault("host", "0.0.0.0")
    gateway.setdefault("port", 18790)

json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

chmod 600 "$JSON_PATH"
mkdir -p "$YUANCLAW_HOME/workspace"

echo "[OK] Generated runtime config: $JSON_PATH"
echo "[OK] Workspace ready: $YUANCLAW_HOME/workspace"
echo "[NEXT] Start gateway: python3 -m yuanclaw gateway"
