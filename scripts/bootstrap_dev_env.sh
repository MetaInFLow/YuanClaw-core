#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TARGET_BRANCH="${1:-}"
if [[ -n "$TARGET_BRANCH" ]]; then
  BRANCH="$TARGET_BRANCH"
else
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
fi

mkdir -p docs/design-doc scripts src tests tools

case "$BRANCH" in
  dev)
    mkdir -p _reference_repo
    if [[ ! -f _reference_repo/README.md ]]; then
      cat > _reference_repo/README.md <<'EOF'
# Local Reference Repositories
This directory is for local reference repos during development.
Everything here is local-only and must not be committed.
EOF
    fi
    ;;
  main|uat|UAT)
    if [[ -d _reference_repo ]]; then
      echo "[WARN] Branch '$BRANCH' should not include _reference_repo."
      echo "[WARN] Remove it manually if you want strict cleanup: rm -rf _reference_repo"
    fi
    ;;
  *)
    echo "[INFO] Branch '$BRANCH' is not in {main, dev, UAT}. Skipping branch-specific policy."
    ;;
esac

echo "[OK] Bootstrap completed on branch '$BRANCH'."
