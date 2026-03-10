# YuanClaw
MetaInFlow’s in-house built claw bot, designed with enterprise-grade security for the AI agent era.

## V0 Bootstrap
- Run `bash scripts/bootstrap_dev_env.sh` after checkout.
- Optional: force a target mode, e.g. `bash scripts/bootstrap_dev_env.sh dev`.
- Folder and branch rules: `docs/project-folder-declaration-v0.md`.

## Dev Onboarding (Reference Repos)
- Switch to `dev` first: `git checkout dev`.
- Initialize workspace: `bash scripts/bootstrap_dev_env.sh dev`.
- See reference options: `bash scripts/reference_repo.sh list`.
- Pull one full reference repo only when needed:
  - `bash scripts/reference_repo.sh pull-full nanobot`
  - `bash scripts/reference_repo.sh pull-full clawx`

Details: `docs/dev-onboarding-v0.md`.

## Deployment (V0)
- First-time runtime setup: `bash setup.sh`
- Deployment guide: `docs/deploy-yuanclaw-v0.md`
