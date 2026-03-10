#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/reference_repo.sh list
  bash scripts/reference_repo.sh pull <project>
  bash scripts/reference_repo.sh pull <project> --full
  bash scripts/reference_repo.sh pull-full <project>
  bash scripts/reference_repo.sh status

Projects:
  nanobot  -> https://github.com/HKUDS/nanobot.git
  clawx    -> https://github.com/ValueCell-ai/ClawX.git
EOF
}

normalize_project() {
  case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
    nanobot) echo "nanobot" ;;
    clawx) echo "clawx" ;;
    *) return 1 ;;
  esac
}

project_url() {
  case "$1" in
    nanobot) echo "https://github.com/HKUDS/nanobot.git" ;;
    clawx) echo "https://github.com/ValueCell-ai/ClawX.git" ;;
    *) return 1 ;;
  esac
}

project_dir() {
  case "$1" in
    nanobot) echo "nanobot" ;;
    clawx) echo "ClawX" ;;
    *) return 1 ;;
  esac
}

ensure_dev_branch() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  if [[ "$branch" != "dev" ]]; then
    echo "[ERROR] This command only runs on branch 'dev'. Current: '$branch'."
    echo "[HINT] Switch first: git checkout dev"
    exit 1
  fi
}

ensure_reference_root() {
  mkdir -p _reference_repo
  if [[ ! -f _reference_repo/README.md ]]; then
    cat > _reference_repo/README.md <<'EOF'
# Local Reference Repositories
This directory is local-only and must not be committed.
Use `bash scripts/reference_repo.sh list` to see available projects.
EOF
  fi
}

cmd_list() {
  cat <<'EOF'
Available reference projects:
- nanobot: https://github.com/HKUDS/nanobot.git
- clawx: https://github.com/ValueCell-ai/ClawX.git

Default pull (shallow):
  bash scripts/reference_repo.sh pull nanobot

Full pull (complete history):
  bash scripts/reference_repo.sh pull-full nanobot
EOF
}

cmd_status() {
  if [[ ! -d _reference_repo ]]; then
    echo "No local _reference_repo directory."
    return
  fi

  for project in nanobot clawx; do
    local dir
    dir="$(project_dir "$project")"
    if [[ -d "_reference_repo/$dir/.git" ]]; then
      local short_sha
      short_sha="$(git -C "_reference_repo/$dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
      echo "$project: present (_reference_repo/$dir @ $short_sha)"
    else
      echo "$project: not pulled"
    fi
  done
}

cmd_pull() {
  local raw_project="${1:-}"
  local full="${2:-}"
  local project
  local url
  local dir

  if [[ -z "$raw_project" ]]; then
    echo "[ERROR] Missing <project>."
    usage
    exit 1
  fi

  if ! project="$(normalize_project "$raw_project")"; then
    echo "[ERROR] Unsupported project: '$raw_project'."
    cmd_list
    exit 1
  fi

  ensure_dev_branch
  ensure_reference_root

  url="$(project_url "$project")"
  dir="$(project_dir "$project")"

  if [[ -d "_reference_repo/$dir/.git" ]]; then
    echo "[OK] Already exists: _reference_repo/$dir"
    return
  fi

  if [[ "$full" == "--full" ]]; then
    git -C _reference_repo clone "$url" "$dir"
    echo "[OK] Full clone completed: _reference_repo/$dir"
  else
    git -C _reference_repo clone --depth 1 "$url" "$dir"
    echo "[OK] Shallow clone completed: _reference_repo/$dir"
  fi
}

command="${1:-help}"
case "$command" in
  list)
    cmd_list
    ;;
  status)
    cmd_status
    ;;
  pull)
    cmd_pull "${2:-}" "${3:-}"
    ;;
  pull-full)
    cmd_pull "${2:-}" "--full"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "[ERROR] Unknown command: '$command'"
    usage
    exit 1
    ;;
esac
