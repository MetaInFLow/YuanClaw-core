#!/usr/bin/env bash
set -euo pipefail

SRC_CONFIG="${1:-$HOME/.nanobot/config.json}"
DST_CONFIG="${2:-$HOME/.yuanclaw/config.json}"

if [[ ! -f "$SRC_CONFIG" ]]; then
  echo "[ERROR] Source config not found: $SRC_CONFIG"
  exit 1
fi

mkdir -p "$(dirname "$DST_CONFIG")"

python3 - "$SRC_CONFIG" "$DST_CONFIG" <<'PY'
import json
import pathlib
import sys

src = pathlib.Path(sys.argv[1]).expanduser()
dst = pathlib.Path(sys.argv[2]).expanduser()

data = json.loads(src.read_text(encoding="utf-8"))


def rewrite(obj):
    if isinstance(obj, str):
        return (
            obj.replace("~/.nanobot", "~/.yuanclaw")
            .replace("/.nanobot/", "/.yuanclaw/")
        )
    if isinstance(obj, list):
        return [rewrite(x) for x in obj]
    if isinstance(obj, dict):
        return {k: rewrite(v) for k, v in obj.items()}
    return obj


out = rewrite(data)
agents = out.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
workspace = defaults.get("workspace")
if not isinstance(workspace, str) or not workspace.strip():
    defaults["workspace"] = "~/.yuanclaw/workspace"

dst.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

chmod 600 "$DST_CONFIG"
mkdir -p "$HOME/.yuanclaw/workspace"

echo "[OK] yuanclaw config generated from nanobot config."
echo "[OK] Source: $SRC_CONFIG"
echo "[OK] Target: $DST_CONFIG"
