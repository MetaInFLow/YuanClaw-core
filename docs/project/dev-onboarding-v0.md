# YuanClaw 新开发者接入流程 (V0)

更新时间: `2026-03-10`

## 1. 分支约束
- `main`: 不使用 `_reference_repo`
- `UAT`: 不使用 `_reference_repo`
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
- 如果在 `main` 或 `UAT` 执行，会报错并提示先切分支。
