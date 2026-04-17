# YuanClaw Project Folder Declaration (V0)

- Version: `V0`
- Last updated: `2026-03-10`
- Status: `living document` (continuous iteration)

## 1. Goal
Define a stable project folder baseline and branch policy for the YuanClaw development kickoff.

## 2. Branch policy (V0)
- `main`: must not include `_reference_repo`.
- `dev`: can use local `_reference_repo` for external reference repositories.
- `uat`: must not include `_reference_repo`.

Notes:
- `_reference_repo` is local-only and gitignored.
- Any reusable outcome from references must be rewritten into this repository and documented in `docs/`.

## 3. Folder baseline (V0)

```text
YuanClaw/
├── docs/
│   ├── README.md
│   ├── architecture/
│   │   └── current-architecture.md
│   ├── project/
│   │   ├── dev-onboarding-v0.md
│   │   └── folder-declaration-v0.md
│   ├── deployment/
│   │   └── deployment-v0.md
│   └── design/
│       ├── README.md
│       ├── designdoc-v0-template.md
│       ├── designdoc-v0-nanobot-basic-replica.md
│       └── optimize-designdoc-v0-template.md
├── scripts/
│   ├── bootstrap_dev_env.sh
│   └── reference_repo.sh
├── config/
│   └── config.json.template
├── setup.sh
├── src/
├── tests/
├── tools/
└── _reference_repo/         # local-only, dev branch usage
```

## 4. `docs/` rules
- All design docs must be stored under `docs/engineering/specs/`.
- Naming convention:
  - `designdoc-v<version>-<topic>.md`
  - `optimize-designdoc-v<version>-<topic>.md`
- Every optimization doc should link to the source design doc it modifies.

## 5. Iteration rules
- Keep this declaration updated when folder strategy changes.
- Any structural change must include:
  - What changed
  - Why it changed
  - Impact on `main/dev/uat`

## 6. Bootstrap command
- Auto-detect current branch:
  - `bash scripts/bootstrap_dev_env.sh`
- Force target branch mode:
  - `bash scripts/bootstrap_dev_env.sh dev`
  - `bash scripts/bootstrap_dev_env.sh main`
  - `bash scripts/bootstrap_dev_env.sh uat`

## 7. Reference pull policy
- Default: do not auto-clone reference repositories.
- New developers should inspect the registry first:
  - `bash scripts/reference_repo.sh list`
- Pull only the needed project:
  - Shallow: `bash scripts/reference_repo.sh pull <project>`
  - Full: `bash scripts/reference_repo.sh pull-full <project>`
- `pull` commands only run on `dev` branch by design.
