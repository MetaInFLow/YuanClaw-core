# 项目初始化开发规范（可复用模板）

> 来源：YuanClaw V0 初始化实践
> 更新时间：2026-03-10
> 使用方式：复制到新项目后，替换 `<项目名>`、分支名、脚本名、目录名。

## 1. 目标与适用范围

- 统一新项目在 0 到 1 阶段的开发协作方式，避免“每个人各写一套”。
- 约束范围覆盖：分支策略、目录基线、初始化脚本、配置安全、文档规则、质量门禁。
- 本文是“启动期规范”，后续迭代要通过文档版本更新维护。

## 2. 分支与发布策略

### 2.1 长期分支（固定）

- `main`：生产基线分支，只接收已通过验收的变更。
- `uat`：验收分支，用于回归和发布前验证。
- `dev`：日常集成分支，功能开发默认合入。

### 2.2 开发分支（短期）

- 命名格式：`codex/<type>/<topic>`。
- `<type>` 建议：`feat`、`fix`、`refactor`、`docs`、`chore`、`spike`、`hotfix`、`release`。
- `<topic>` 使用小写 kebab-case（只允许字母/数字/`-`）。
- 一个分支只做一类变更，避免功能、修复、文档混改。

### 2.3 Tag 规则（SemVer）

- 使用 `<major>.<minor>.<patch>`。
- 开发里程碑：`x.y.z-dev.N`（打在 `dev`）。
- 验收候选：`x.y.z-uat.N`（打在 `uat`）。
- 正式发布：`x.y.z`（打在 `main`）。

## 3. 目录基线（启动期）

建议初始化以下目录结构：

```text
<project>/
├── docs/
│   ├── architecture/
│   ├── project/
│   ├── deployment/
│   └── design/
├── scripts/
├── config/
├── src/                 # 或主包目录（如 yuanclaw/）
├── tests/
├── tools/
└── _reference_repo/     # 本地参考目录（可选）
```

目录治理要求：

- `_reference_repo/` 必须 gitignore，仅作本地参考，不进入版本库。
- 结构变更必须同步更新 `docs/project/folder-declaration-*.md`。
- 外部参考仓的可复用成果必须“重写后入库”，并在 `docs/` 记录来源与取舍。

## 4. 新成员初始化流程

标准流程：

1. 拉取代码并进入仓库根目录。
2. 切到 `dev`：`git checkout dev`。
3. 执行 bootstrap：`bash scripts/bootstrap_dev_env.sh dev`。
4. 查看参考仓清单：`bash scripts/reference_repo.sh list`。
5. 按需拉取参考仓（可选）：`bash scripts/reference_repo.sh pull-full <project>`。

设计约束：

- bootstrap 脚本要支持“自动识别当前分支”和“强制指定分支模式”两种入口。
- 对分支策略冲突要直接报错或警告（例如在 `main/uat` 禁止拉取参考仓）。

## 5. 外部参考仓策略（可选能力）

- 默认不自动下载大型参考仓。
- 参考仓拉取行为只允许在 `dev` 分支执行。
- 必须提供统一入口脚本（如 `scripts/reference_repo.sh`）：
  - `list`：查看可选仓列表。
  - `pull`：浅克隆。
  - `pull-full`：完整历史克隆。
  - `status`：查看本地拉取状态。

## 6. 配置与安全基线

- 运行时配置使用“用户目录配置文件”，避免把敏感信息写入仓库。
- 模板文件放在仓库内（如 `config/config.json.template`）。
- 首次运行通过 setup 脚本生成配置（如 `~/.<project>/config.json`）。
- 配置文件权限建议 `600`。
- API Key、Token 一律禁止提交到 Git。
- 工具默认限制在工作区内执行（如 `restrictToWorkspace = true`）。

## 7. 文档规范

- `docs/architecture/`：架构现状和边界说明（Living Doc）。
- `docs/project/`：分支、目录、协作流程等工程规范。
- `docs/deployment/`：部署与运行步骤。
- `docs/design/`：设计方案与优化记录。

Design Doc 命名建议：

- 新方案：`designdoc-v<version>-<topic>.md`。
- 优化方案：`optimize-designdoc-v<version>-<topic>.md`。
- 优化文档要显式引用被优化的原始方案。

## 8. 代码质量门禁（最小可执行）

以 YuanClaw 的 Python 基线为例：

- Python：`>=3.11`。
- Lint：`ruff`（行宽 100，规则按项目 `pyproject.toml`）。
- Test：`pytest` + `pytest-asyncio`。

建议在 CI 与本地统一执行：

```bash
ruff check .
pytest
```

## 9. 推荐协作流转

1. 从 `dev` 创建开发分支：`git checkout -b codex/<type>/<topic> dev`。
2. 开发完成后合回 `dev`，必要时打 `x.y.z-dev.N`。
3. 从 `dev` 合入 `uat` 做验收并打 `x.y.z-uat.N`。
4. 验收通过后合入 `main`，打正式版本 `x.y.z`。

## 10. 新项目落地检查清单

- [ ] 已定义长期分支职责（`main/uat/dev`）。
- [ ] 已定义短期分支命名规则。
- [ ] 已定义 Tag 策略并与分支关联。
- [ ] 已建立目录基线与 folder declaration 文档。
- [ ] 已提供 bootstrap 与 setup 脚本。
- [ ] 已配置模板化配置文件与敏感信息隔离。
- [ ] 已将 `_reference_repo` 加入 `.gitignore`（若启用该机制）。
- [ ] 已建立 docs 四层结构（architecture/project/deployment/design）。
- [ ] 已确定 lint/test 的最低门禁与执行命令。
- [ ] 已在 README 写明“新成员首日接入流程”。
