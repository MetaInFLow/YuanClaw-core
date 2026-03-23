# YuanClaw 新开发者接入流程 (V0)

更新时间: `2026-03-10`

## 1. 分支约束
- `main`: 不使用 `_reference_repo`
- `uat`: 不使用 `_reference_repo`
- `dev`: 可以使用 `_reference_repo` 做外部仓库参考

## 2. 新开发者首次接入
1. 拉代码并进入仓库
2. 切到 `dev` 分支: `git checkout dev`
3. 初始化本地结构: `bash scripts/bootstrap_dev_env.sh dev`
4. 查看可选 reference 项目: `bash scripts/reference_repo.sh list`

说明:
- 默认不会自动下载大型 reference 仓库代码。
- `_reference_repo` 是本地目录，已在 `.gitignore` 中忽略。
- 只要 `_reference_repo` 不进入 Git 跟踪即可，是否保留在本地由开发者自行决定。

## 3. 按需下载 reference 仓库
可选项目:
- `nanobot` -> `https://github.com/HKUDS/nanobot.git`
- `clawx` -> `https://github.com/ValueCell-ai/ClawX.git`

按项目名下载:
- 浅克隆（默认）: `bash scripts/reference_repo.sh pull <project>`
- 完整拉取（完整历史）: `bash scripts/reference_repo.sh pull-full <project>`

示例:
- `bash scripts/reference_repo.sh pull-full nanobot`
- `bash scripts/reference_repo.sh pull-full clawx`

## 4. 状态检查
- `bash scripts/reference_repo.sh status`

## 5. 常见限制
- `pull`/`pull-full` 只能在 `dev` 分支执行。
- 如果在 `main` 或 `uat` 执行，会报错并提示先切分支。

## 6. Branch 命名规范（统一）
### 6.1 长期分支（固定）
- `main`: 生产基线分支（只接收已通过 uat 的变更）
- `uat`: 验收分支（用于回归和发布前验证）
- `dev`: 日常集成分支（功能开发默认合入这里）

### 6.2 开发分支（短期）
- 命名格式: `codex/<type>/<topic>`
- `<type>` 允许值: `feat`、`fix`、`refactor`、`docs`、`chore`、`spike`、`hotfix`、`release`
- `<topic>` 使用小写 kebab-case（仅字母/数字/`-`）

示例:
- `codex/feat/telegram-reply-policy`
- `codex/fix/gateway-allow-from-default`
- `codex/docs/branch-tag-convention`
- `codex/release/0.0.2`

### 6.3 命名约束
- 禁止中文、空格、下划线和大写字母。
- 分支名要表达“改什么”，不要只写日期或个人名。
- 一个分支只做一类变更（功能、修复、文档不要混在同一个分支）。

## 7. Tag 命名规范（统一）
### 7.1 版本格式
- 使用 SemVer: `<major>.<minor>.<patch>`
- 当前项目处于 `0.x` 阶段，通常递增 `minor/patch`

### 7.2 Tag 类型与落点
- `x.y.z-dev.N`: 开发里程碑，打在 `dev`（建议在准备合并前打）
- `x.y.z-uat.N`: 验收候选，打在 `uat`
- `x.y.z`: 正式发布，打在 `main`

示例:
- `0.0.2-dev.1`
- `0.0.2-uat.1`
- `0.0.2`

### 7.3 兼容说明
- 既有 tag `0.0.1` 作为历史里程碑保留，不追溯改名。

## 8. 推荐流转（简版）
1. 从 `dev` 拉开发分支：`git checkout -b codex/<type>/<topic> dev`
2. 开发完成后合回 `dev`，必要时打 `x.y.z-dev.N`
3. 从 `dev` 合入 `uat` 做验收并打 `x.y.z-uat.N`
4. 验收通过后合入 `main`，打正式 `x.y.z`
