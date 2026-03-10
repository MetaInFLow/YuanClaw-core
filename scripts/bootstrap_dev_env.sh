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

mkdir -p docs/project docs/deployment docs/design scripts src tests tools

case "$BRANCH" in
  dev)
    mkdir -p _reference_repo
    cat > _reference_repo/README.md <<'EOF'
# Local Reference Repositories
This directory is for local reference repos during development and is local-only.
Do not commit anything under this folder.

Available pull commands:
- List projects: bash scripts/reference_repo.sh list
- Pull one project (shallow): bash scripts/reference_repo.sh pull <project>
- Pull one project (full): bash scripts/reference_repo.sh pull-full <project>
EOF
    echo "[INFO] Dev onboarding:"
    echo "       1) bash scripts/bootstrap_dev_env.sh dev"
    echo "       2) bash scripts/reference_repo.sh list"
    echo "       3) bash scripts/reference_repo.sh pull-full <project>   # if full reference is required"
    ;;
  main|uat|UAT)
    if [[ -d _reference_repo ]]; then
      echo "[WARN] Branch '$BRANCH' should not include _reference_repo."
      echo "[WARN] _reference_repo is gitignored, but you can remove it locally if needed."
    fi
    ;;
  *)
    echo "[INFO] Branch '$BRANCH' is not in {main, dev, UAT}. Skipping branch-specific policy."
    ;;
esac

echo "[OK] Bootstrap completed on branch '$BRANCH'."
