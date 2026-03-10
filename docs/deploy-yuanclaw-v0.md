# YuanClaw Deployment Steps (V0)

## Goal
Bootstrap YuanClaw locally by reusing `~/.nanobot/config.json` as a reference, while keeping YuanClaw config isolated at `~/.yuanclaw/config.json`.

## Steps
1. Enter repo root.
   - `cd /Users/anthonyf/projects/metainflow/YuanClaw`
2. Ensure config bootstrap script is executable.
   - `chmod +x scripts/setup_yuanclaw_config_from_nanobot.sh`
3. Generate YuanClaw config from nanobot config.
   - `bash scripts/setup_yuanclaw_config_from_nanobot.sh`
4. Verify runtime entry.
   - `python3 -m yuanclaw --version`
   - `python3 -m yuanclaw --help`
5. Verify config path and workspace path migration.
   - `python3 - <<'PY'\nimport json, pathlib\np=pathlib.Path.home()/'.yuanclaw'/'config.json'\nd=json.loads(p.read_text())\nprint('config_exists=',p.exists())\nprint('workspace=',d.get('agents',{}).get('defaults',{}).get('workspace'))\nPY`

## Re-deploy Test (Clean State)
1. Remove YuanClaw local runtime dir.
   - `rm -r ~/.yuanclaw`
2. Re-run steps 3-5.
3. Success criteria:
   - `~/.yuanclaw/config.json` exists
   - workspace path is `~/.yuanclaw/workspace` (or migrated to `.yuanclaw` path)
   - `python3 -m yuanclaw --help` exits successfully
