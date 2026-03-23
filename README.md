# YuanClaw-core
Multi-channel personal AI assistant runtime — the brain of YuanClaw

## V0 Bootstrap
- Run `bash scripts/bootstrap_dev_env.sh` after checkout.
- Optional: force a target mode, e.g. `bash scripts/bootstrap_dev_env.sh dev`.
- Folder and branch rules: `docs/project/folder-declaration-v0.md`.

## Dev Onboarding (Reference Repos)
- Switch to `dev` first: `git checkout dev`.
- Initialize workspace: `bash scripts/bootstrap_dev_env.sh dev`.
- See reference options: `bash scripts/reference_repo.sh list`.
- Pull one full reference repo only when needed:
  - `bash scripts/reference_repo.sh pull-full nanobot`
  - `bash scripts/reference_repo.sh pull-full clawx`

Details: `docs/project/dev-onboarding-v0.md`.

## Deployment (V0)
- First-time runtime setup (interactive wizard): `bash setup.sh`
- Deployment guide: `docs/deployment/deployment-v0.md`
