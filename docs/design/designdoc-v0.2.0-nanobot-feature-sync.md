# YuanClaw Core - nanobot v0.2.0 Feature Sync 开发文档

- 文档版本: `v0.2.0`
- Status: `active`
- Release target: `0.2.0`
- Owner: `YuanClaw`
- Last updated: `2026-06-01`
- 目标分支: `dev`
- 适用仓库: `MetaInFLow/YuanClaw-core`
- 上游参考: `_reference_repo/nanobot`
- 上游基线: `nanobot main @ 15c6abc9`
- 上游版本: `nanobot-ai 0.2.0`
- 本轮开发边界: `Agent Core only`

## 1. 背景

已将 `_reference_repo/nanobot` 从 `ce5272c1` 快进拉取到 `15c6abc9`。这次更新不是单点 bugfix，而是从 `0.1.4.post4` 系列跨到 `0.2.0` 的大版本能力包。

上游 `README.md` 对 `v0.2.0` 的主声明是：
- `/goal` 支持跨轮持续目标
- WebUI 随 Python wheel 打包分发
- 图片生成端到端闭环
- 新增 provider 与 `fallback_models`
- Agent loop 进行真实重构

YuanClaw 当前基线仍是 `0.1.4.post4`，且缺少本次上游新增的 Agent Core 关键模块，例如 `session/goal_state.py`、`agent/progress_hook.py`、`agent/model_presets.py`、`agent/tools/apply_patch.py`、`agent/tools/exec_session.py`、`agent/tools/long_task.py`、`agent/tools/image_generation.py`、`providers/factory.py`、`providers/fallback_provider.py`、`providers/image_generation.py`、`security/workspace_access.py`、`security/workspace_policy.py`、`apps/cli/*`、`pairing/*`、`channels/signal.py`。

## 2. 目标

将 nanobot `0.2.0` 的 Agent Core 新能力按 YuanClaw 命名与路径约束迁移进来，并保持现有 `yuanclaw` CLI、`~/.yuanclaw` 运行目录、包名与文案策略不回退。

本次目标是先完成可验收的 core feature sync，不做 WebUI，不做 Studio 桌面架构重写。上游 WebUI 相关变更只作为后端协议、session metadata、media artifact、workspace policy 的来源参考；前端工程与打包暂不迁移。

## 3. 范围

### 3.1 P0 必须同步

1. Agent loop / runner 重构
   - 对齐上游 `nanobot/agent/loop.py`、`runner.py`、`progress_hook.py`、`autocompact.py`、`subagent.py` 的核心行为。
   - 保留 `yuanclaw.*` import、日志命名和运行目录。
   - 补齐并迁移对应测试，优先覆盖 runner、session persistence、progress、runtime refresh、task cancel。

2. Sustained goal
   - 新增 `yuanclaw/session/goal_state.py`。
   - 新增或迁移 `long-goal` skill。
   - 支持会话 metadata 中的 `goal_state`，并兼容上游遗留键的读取逻辑。
   - Agent runtime context 能在 active goal 下稳定注入目标摘要。

3. Core channel / API 后端契约
   - 迁移上游 WebSocket channel 中属于 Agent Core 的能力：token issuance、安全监听约束、session replay、media serving、workspace scope、goal/progress event。
   - 不迁移浏览器前端源码，不把 settings/sidebar/thread UI API 作为本轮目标；只有 core runtime 必须依赖的后端 helper 才迁移。
   - `0.0.0.0` 监听必须要求 token 或 `tokenIssueSecret`，与上游安全策略一致。

4. Workspace security
   - 新增 `yuanclaw/security/workspace_access.py` 与 `workspace_policy.py`。
   - 文件、媒体、workspace 选择、reference image 等入口必须通过 workspace 边界检查。
   - 保留现有 `tools.restrictToWorkspace` 语义，并补齐缺失测试。

5. Provider registry 与 fallback
   - 迁移 provider factory、fallback provider、model presets、extra body/api type 等配置。
   - 补齐 Bedrock、Novita、LongCat、StepFun/StepPlan、Zhipu、image provider 相关能力。
   - 优先迁移 native provider path，避免新增 provider 继续强依赖 `litellm`；迁移过程中发现的既有 `litellm` 长尾路径作为 release exception 记录，并纳入单独清理计划。
   - 当前已完成 provider factory/fallback、registry/config 暴露、OpenAI-compatible native provider、Bedrock native provider 与 Anthropic native provider 最小闭环；Zhipu / DashScope / Moonshot / MiniMax / Gemini 等特殊协议或兼容性待确认 provider 暂留 `LiteLLMProvider` transitional path，不阻塞本轮 Agent Core v0.2.0 release。

### 3.2 P1 必须同步

1. Image generation
   - 新增 `generate_image` tool、image provider 抽象、artifact 保存、reference image 校验。
   - CLI/channel 调用能选择 aspect ratio、上传或引用 reference image。
   - 生成结果以 assistant media/artifact 回放，并能作为后续编辑参考图。

2. CLI Apps 与 MCP presets 后端
   - 迁移 `yuanclaw/apps/cli/*`、MCP preset normalization 与 runtime attachment 逻辑。
   - `@app` / MCP preset mention 能转换为 session attachment/runtime hint。
   - 设置或配置变更后工具发现能刷新，不需要重启整个 Python 进程。

3. Signal channel
   - 新增 `yuanclaw/channels/signal.py`、pairing store、配置 schema、文档。
   - 支持 signal-cli daemon、DM pairing、allowlist、attachment directory、Markdown text style 映射。

4. Core tool runtime enhancements
   - 迁移 `apply_patch`、`exec_session`、`long_task`、tool loader scopes、runtime state、path utils 等核心工具能力。
   - 保留现有 filesystem/shell/web/mcp/message/cron 工具行为兼容。

### 3.3 P2 稳定性与文档

1. Core 文档结构对齐
   - 将上游拆分后的 `docs/configuration.md`、`docs/chat-apps.md`、`docs/image-generation.md`、`docs/openai-api.md` 中与 Agent Core 有关的内容按 YuanClaw 命名重写。
   - 删除或重定向过期的 `nanobot` 文档引用。

2. 配置迁移说明
   - 明确 `~/.nanobot` 不自动迁移到 `~/.yuanclaw`。
   - 多实例、workspace、media、session、memory、cron 路径全部以 YuanClaw 当前约定为准。

3. UI 边界记录
   - 在本设计文档中明确：WebUI 前端、WebUI wheel 打包、Studio/Tauri 整合均不在本轮开发范围。

## 4. 非目标

- 不保留 `nanobot` CLI 别名。
- 不迁移用户本机 `~/.nanobot` 历史配置。
- 不迁移 `webui/` 前端工程。
- 不迁移 `yuanclaw/web/dist` 打包产物。
- 不新增 WebUI wheel build hook。
- 不把 WebUI 直接改造成 Tauri Studio。
- 不在本轮引入新的产品定位或品牌文案。
- 不做与上游无关的大规模模块重构。

## 5. 代码映射

| 上游路径 | YuanClaw 目标路径 | 优先级 | 说明 |
|---|---|---:|---|
| `nanobot/agent/*` | `yuanclaw/agent/*` | P0 | loop、runner、progress、auto compact、subagent |
| `nanobot/session/goal_state.py` | `yuanclaw/session/goal_state.py` | P0 | `/goal` 状态读取与 runtime context |
| `nanobot/channels/websocket.py` | `yuanclaw/channels/websocket.py` | P0 | Core WebSocket/API 后端能力，剔除纯 UI 面 |
| `nanobot/security/*` | `yuanclaw/security/*` | P0 | workspace scope 与安全策略 |
| `nanobot/providers/*` | `yuanclaw/providers/*` | P0/P1 | provider factory、fallback、image provider |
| `nanobot/agent/tools/image_generation.py` | `yuanclaw/agent/tools/image_generation.py` | P1 | `generate_image` tool |
| `nanobot/apps/cli/*` | `yuanclaw/apps/cli/*` | P1 | CLI Apps registry/service |
| `nanobot/pairing/*` | `yuanclaw/pairing/*` | P1 | Signal pairing |
| `nanobot/channels/signal.py` | `yuanclaw/channels/signal.py` | P1 | Signal channel |
| `nanobot/skills/long-goal` | `yuanclaw/skills/long-goal` | P0 | 持续目标 skill |
| `nanobot/skills/image-generation` | `yuanclaw/skills/image-generation` | P1 | 图片生成 skill |
| `nanobot/agent/tools/apply_patch.py` | `yuanclaw/agent/tools/apply_patch.py` | P1 | 文件编辑工具 |
| `nanobot/agent/tools/exec_session.py` | `yuanclaw/agent/tools/exec_session.py` | P1 | 长 shell session |
| `nanobot/agent/tools/long_task.py` | `yuanclaw/agent/tools/long_task.py` | P0 | sustained goal 工具 |

## 6. 开发切片

### Slice A: 上游差异盘点与机械映射

- 生成 `nanobot -> yuanclaw` 文件级映射清单。
- 对所有新增文件做 import/路径/品牌替换。
- 跑静态扫描，禁止 `import nanobot`、`from nanobot`、`~/.nanobot`、`nanobot-ai` 非历史说明残留。

验收:
- `rg "from nanobot|import nanobot" yuanclaw tests` 无结果。
- `python -m yuanclaw --help` 与 `yuanclaw --help` 可运行。

### Slice B: Agent runtime 与 sustained goal

- 迁移 runner/loop/session 相关变更。
- 补齐 `goal_state` metadata、runtime lines、WebSocket goal event。
- 迁移 runner 相关测试。

验收:
- 单元测试覆盖 active goal、completed goal、legacy metadata 读取。
- 长任务执行中，runner 不因请求超时提前退出。
- 同 session 并发 turn 被锁保护，不出现 session 文件交错写入。

### Slice C: Core channel/API 后端与安全

- 迁移 WebSocket channel 的 HTTP route 和 token issuance。
- 迁移 workspace access/policy。
- 建立 token、sessions、media、workspace API 的测试。

验收:
- `yuanclaw gateway` 开启 websocket 后，核心 session/media/workspace API 返回合法 JSON。
- `host=0.0.0.0` 且无 token 配置时拒绝启动。
- media/reference image 不能读取 workspace/media 目录外文件。

### Slice D: Core tool runtime

- 迁移 `apply_patch`、`exec_session`、`long_task`、tool loader、runtime state、path utils。
- 与既有 filesystem/shell/mcp/message 工具注册机制合并。
- 补齐 tool schema、allow pattern、session stdin/stdout、patch 边界测试。
- `exec` 的 `working_dir` 必须受 workspace policy 约束，包含 one-shot 与 `yield_time_ms` 长 session 两条路径。
- 长 exec session 必须按 AgentLoop 实例隔离，不能跨 Studio/API session list、poll、stdin、terminate。
- `exec` 只透传安全 allowlist 环境变量，禁止把任意本地 secret/env 泄漏给模型触发的 shell 命令。
- `apply_patch` 多文件写入必须在可写性预检失败或写入异常时保持无部分落盘，并保留既有 CRLF 文件换行风格。

验收:
- `apply_patch` 只能修改 workspace policy 允许的文件。
- `exec_session` 支持启动、轮询、stdin、关闭，并能正确处理超时。
- `exec_session` 不允许通过 `working_dir` 跳出 workspace；session 只在创建它的 AgentLoop 中可见。
- `exec` 不透传任意环境变量；新增 env key 需要显式加入安全 allowlist。
- `apply_patch` 的多文件失败不会留下半应用状态；append 到 CRLF 文件不产生整文件 LF 化。
- `long_task` 写入 session goal metadata，`complete_goal` 能关闭 active goal。
- tool loader 能发现内置工具并避免重复注册。

### Slice E: Image generation

- 迁移 tool、provider、artifact、reference image 控制。
- 支持至少一个可 mock 的 provider 单测路径。
- 文档覆盖 provider config、reference image 限制、artifact 存储。

验收:
- `tools.imageGeneration.enabled=false` 时不注册 `generate_image`。
- 开启后 tool schema 中包含 prompt、aspect_ratio、reference_images、count。
- reference image 越界路径被拒绝。
- mock provider 返回图片时，artifact 写入 media/generated 并在 session 中可回放。

### Slice F: CLI Apps / MCP presets / Signal

- 迁移 CLI app registry、配置读取/刷新、mention normalization。
- 迁移 MCP preset setup 与 capability mentions。
- 迁移 Signal channel 与 pairing store。

验收:
- 配置中保存 CLI app 后，下一轮工具发现能看到对应 app。
- MCP preset mention 能写入 session attachment，并在 runtime context 中可见。
- Signal DM pairing、allowlist、attachment 目录配置均有测试覆盖。

## 7. 全局验收目标

### 7.1 功能验收

- `yuanclaw onboard`、`yuanclaw agent`、`yuanclaw gateway` 保持可运行。
- `/goal` 或等价持续目标命令能跨 turn 持续显示，并在完成后清理状态。
- 图片生成能从 CLI/channel/tool 调用触发，生成结果可保存、可作为后续 reference。
- CLI Apps 与 MCP presets 能通过配置/session attachment 影响 agent runtime。
- Signal 作为新增 channel 能完成最小收发链路。

### 7.1.1 当前已达成的 Agent Core 验收

- Sustained goal metadata、runtime context、`long_task` / `complete_goal`、goal continuation loop、active goal runner wall timeout disable、同 session direct turn 串行化已有 focused tests。
- Runtime refresh 已覆盖 provider / agent / session manager / tool snapshot 更新。
- Idle session auto-compact 已新增 `yuanclaw/agent/autocompact.py`，通过 `agents.defaults.compaction.idleCompactAfterMinutes` / `sessionTtlMinutes` 配置，后台压缩过期 session，保存 `_last_summary` metadata，并在下一轮 runtime context 注入 previous conversation summary。
- Progress hook 已覆盖 structured tool start/end events 与 `apply_patch` file edit start/end/error events，并保持旧 `on_progress(content, tool_hint=...)` 回调兼容。
- Runner loop 已覆盖 blank final response retry 与 `finish_reason=length` continuation recovery，避免空回复或被 max_tokens 截断时直接结束。
- Subagent `spawn` 已继承当前 turn 的 workspace scope，并在子任务工具执行期间绑定相同 workspace scope，避免 scoped workspace 下回落默认 workspace；subagent LLM call 已接入 per-session runner wall timeout，active sustained goal 可禁用该 timeout；`agents.defaults.maxConcurrentSubagents` 已支持配置并默认限制为 `1`，超过上限时拒绝启动新 subagent。
- Workspace policy 已接入 filesystem、shell working dir、media/reference image、image generation reference path；`/ws/chat` payload 的 `workspaceScope` / `workspace_scope` 已转发并在 Studio turn 生效。
- Core API 已支持 gateway token/token issue、session read-only replay、signed media、signed media Range、WebSocket `goal_status` / `turn_end`、structured progress fields。
- Provider factory/fallback、model preset/config 字段、upstream provider registry entries、OpenAI-compatible native provider、Bedrock native provider、Anthropic native provider 已有最小测试；OpenRouter / VolcEngine / BytePlus 等 OpenAI-compatible gateways 已绕过 LiteLLM 走 native provider。
- Image generation tool/provider/artifact/reference image boundary 已有最小测试。
- `apply_patch`、long exec session、stdin、per-loop/per-conversation session isolation、safe env allowlist、patch atomicity/CRLF preservation 已有最小测试。
- CLI Apps / MCP presets 已支持 attachment normalization、session persistence、runtime context injection、WebSocket metadata forwarding、local installed-app execution；CLI Apps core service 已支持本地 catalog、install、settings update、uninstall、entry point test；MCP presets core service 已支持内置 preset catalog、enable/remove、required settings 校验、managed stdio cwd、dependency/test connection 与 configured/connected/stale runtime state hints。
- Signal channel 已支持 config、pairing store、DM/group allowlist、attachment directory boundary、JSON-RPC send、daemon health check、SSE event receive、reconnect backoff、未授权 DM pairing code 自动回复与 pairing CLI 最小链路。

### 7.1.2 当前未达成 / 后续验收目标

- Runner/progress 尚未完整拆出上游 `runner.py`、`progress_hook.py` 模块形态；`runner_wall_llm_timeout_s` 已接入 AgentLoop/process_direct 真实 LLM call 路径，structured tool progress 与 idle autocompact 已落地。
- 仍需补 runner 级测试：progress hook 更完整的 reasoning segment、checkpoint、pending injection、microcompact 等行为；empty/length recovery 已有最小测试。
- Subagent 尚未完整接入上游 `AgentRunner` / ToolLoader scope / status checkpoint；当前已补 workspace scope 传递与绑定、runner wall timeout、max concurrent subagents 配置的最小安全闭环。
- 非 LiteLLM 原生 provider 迁移仍未全部完成；OpenAI-compatible native、Bedrock native、Anthropic native 最小闭环已落地，OpenRouter / VolcEngine / BytePlus 已切到 native path，Zhipu / DashScope / Moonshot / MiniMax / Gemini 等非 OpenAI-compatible 或需特殊协议/兼容性确认的 provider 仍保留 LiteLLM transitional path。本轮 Agent Core v0.2.0 release 将其作为明确 release exception，而不是宣称完全移除 LiteLLM。
- CLI Apps core service 已完成本地 catalog/install/settings/uninstall/test entry point；HTTP API/WebUI wiring、远端 registry refresh、远端安装命令执行不在当前 Agent Core slice 内。
- MCP presets 已有 Python core service 与 configured / connected / stale runtime state hints；HTTP API/WebUI wiring、完整上游 preset 列表和 OAuth preset UX 不在当前 Agent Core slice 内。
- Signal 尚未迁移 typing indicator、textStyle range、长消息 split 与更完整 markdown 转换。
- `docs/api/current-supported-interfaces.md` 已补当前最小接口；后续每个 Core API/Agent Core slice 需要同步更新。

### 7.2 兼容验收

- 所有 public import 使用 `yuanclaw.*`。
- 所有 CLI 示例、文档、配置路径使用 `yuanclaw`、`yuanclaw-ai`、`~/.yuanclaw`。
- 不新增 `nanobot` 兼容命令。
- 旧有 Telegram、Slack、Discord、Feishu、Email、QQ、Matrix、DingTalk、WhatsApp 测试不因 feature sync 回归。

### 7.3 安全验收

- WebSocket LAN 访问必须有 token 或 token issue secret。
- Web fetch、media route、reference image、file tools 均不能越过 workspace policy。
- API key 仍只允许通过 config/env 注入，不进入 session transcript 或日志。
- Signal、MSTeams、DingTalk 等 outbound media URL 必须保留上游安全限制。

### 7.4 打包验收

- `pip install -e .` 可用于 Python core 开发。
- `python -m build` 不依赖前端构建工具。
- sdist/wheel 中包含必要的 skills、templates、bridge。
- 新增 Python package data 不漏包。

### 7.5 测试门禁

合并前必须至少通过：

```bash
python -m compileall yuanclaw tests
pytest
python -m build
rg "from nanobot|import nanobot" yuanclaw tests
rg "~/.nanobot|nanobot-ai|nanobot " README.md docs yuanclaw tests pyproject.toml
```

其中第二个 `rg` 允许在本设计文档的上游引用段落出现，但不允许出现在用户操作示例、CLI 输出、配置模板和运行时代码中。

## 8. 当前实现进度

### 2026-05-31

- 已完成 Slice B 的 sustained goal 最小闭环：
  - 新增 `yuanclaw/session/goal_state.py`
  - 新增 `long_task` / `complete_goal` 工具
  - Runtime Context 注入 active goal metadata
  - AgentLoop 注册并设置 goal tools 上下文
  - active goal 下普通最终回答会注入 continuation message，直到 goal 完成或达到 max iterations
  - `runner_wall_llm_timeout_s` 接入 AgentLoop/process_direct 的真实 LLM call 路径；active goal 返回 `0.0` 禁用外层 wall timeout，避免长任务被普通请求超时截断
  - `process_direct` 接入同一 per-session lock；Studio/CLI direct 同 session 并发 turn 会在构建 history 前串行化，避免第二轮看不到第一轮已持久化消息
  - 新增内置 `long-goal` skill
- 已覆盖测试：
  - `tests/test_goal_state.py`
  - `tests/test_long_task_tool.py`
  - `tests/test_goal_continuation_loop.py` 中 active goal continuation、inactive exit、active goal timeout disable、inactive timeout、同 session `process_direct` 并发串行化 case
  - `tests/test_context_prompt_cache.py` 中 active goal runtime context case
- 下一批优先级：
  1. workspace security / media boundary
  2. provider factory 与 fallback
  3. image generation core tool/provider
  4. core channel/API 后端安全约束

### 2026-05-31 / Workspace security progress

- 已完成 Slice C/P0 的 workspace policy 最小闭环：
  - 新增 `yuanclaw/security/workspace_policy.py`
  - 新增 `yuanclaw/security/workspace_access.py`
  - 文件工具改为读取当前 turn 的 `WorkspaceScope`
  - AgentLoop 在普通 message turn 中解析、持久化、绑定并 reset workspace scope
  - 支持 WebSocket/Studio message metadata 中的 `workspace_scope` 覆盖当前 turn/project root；`/ws/chat` 已接受 `workspaceScope` / `workspace_scope` payload 并转发到 AgentLoop metadata
  - ContextBuilder 的 inbound media image 读取接入同一 workspace policy，restricted scope 下拒绝越界 media path
- 已覆盖测试：
  - `tests/test_workspace_security.py`
  - `tests/test_studio_api.py` 中 `/ws/chat` workspace scope 转发 case
  - 相关回归：`tests/test_filesystem_tools.py`
  - 相关回归：`tests/test_context_prompt_cache.py`
- 下一批优先级：
  1. reference image boundary 在 image generation tool/provider 落地时复用同一 policy
  2. provider factory 与 fallback
  3. image generation core tool/provider

### 2026-05-31 / Core API security progress

- 已完成 Slice C/P0 的 core API token 与 LAN 安全约束最小闭环：
  - `GatewayConfig` 新增 `token`、`tokenIssuePath`、`tokenIssueSecret`、`tokenTtlS`
  - `CoreRuntime` 启动与热配置预检时会拒绝 `0.0.0.0` / `::` 无鉴权材料的绑定
  - HTTP API 在配置 token 或 token issue secret 后要求 `Authorization: Bearer <token>`、`X-YuanClaw-Auth` 或 query `token`
  - 新增 token issue endpoint，使用 `tokenIssueSecret` 签发短期 bearer token
  - `/ws/chat` 与 `/ws/events` 在 accept 前校验 static token 或已签发 token
  - `/health` 保持免鉴权，便于本地存活探测
- 已覆盖测试：
  - `tests/test_studio_api.py` 中 public bind guard、camelCase config、HTTP token、token issue endpoint、WebSocket token case
  - 相关回归：`tests/test_commands.py`
- 下一批优先级：
  1. provider factory 与 fallback
  2. image generation core tool/provider
  3. Core WebSocket/session replay/media route 中剩余非 UI 后端协议

### 2026-05-31 / Runtime refresh progress

- 已完成 Runner/runtime refresh 的最小保护性验收：
  - `CoreRuntime.apply_config` 会用新 config 重建 provider、agent、session manager、cron、channels
  - provider/model 变更后 `runtime.provider`、`runtime.agent.provider`、`runtime.agent.model` 会同步更新
  - `tools.cliApps.enabled`、`tools.imageGeneration.enabled` 等工具配置变更后，`AgentLoop` 工具集合会随新 runtime snapshot 刷新
- 已覆盖测试：
  - `tests/test_studio_api.py::test_core_runtime_apply_config_refreshes_provider_and_tool_snapshot`
- 剩余 Runner 工作：
  1. progress hook 行为尚未完整迁移成上游 runner/progress_hook 形态
  2. `runner.py` 模块形态仍未拆出；当前先在 `AgentLoop` 保持行为闭环

### 2026-05-31 / Progress hook progress

- 已完成 progress hook 的 structured tool event 最小闭环：
  - `AgentLoop` 在 tool call 开始时向支持 `tool_events` 参数的 progress callback 发送 `phase=start` 事件
  - tool call 结束后发送 `phase=end` 事件；工具返回 `Error...` 时发送 `phase=error`
  - `apply_patch` tool call 会向支持 `file_edit_events` 参数的 progress callback 发送 file edit `phase=start/end/error` 事件
  - 不支持 `tool_events` 的旧 callback 仍只收到原有纯文本 progress/tool hint，不会收到空 progress frame
- 已覆盖测试：
  - `tests/test_message_tool_suppress.py::TestMessageToolSuppressLogic::test_progress_includes_structured_tool_start_and_finish_events`
  - `tests/test_message_tool_suppress.py::TestMessageToolSuppressLogic::test_progress_includes_apply_patch_file_edit_events`
  - `tests/test_message_tool_suppress.py::TestMessageToolSuppressLogic::test_progress_marks_apply_patch_file_edit_errors`
  - `tests/test_message_tool_suppress.py::TestMessageToolSuppressLogic::test_progress_hides_internal_reasoning` 继续覆盖旧 callback 兼容与 `<think>` 隐藏
- 剩余 progress/autocompact 工作：
  1. file edit events 已覆盖 `apply_patch` call-level start/end/error；尚未做逐 chunk live delta 追踪
  2. reasoning segment open/end 更完整的 progress hook 模块形态尚未拆出
  3. idle session auto-compact 已按上游 `AutoCompact` 独立模块迁移，但尚未迁移完整 runner checkpoint / microcompact 体系

### 2026-05-31 / AutoCompact progress

- 已完成 idle session auto-compact 的 Agent Core 行为闭环：
  - 新增 `yuanclaw/agent/autocompact.py`，迁移上游 `AutoCompact` 的 TTL 判断、过期 session 后台归档、in-memory summary cache、metadata cold path summary 读取
  - 新增 `agents.defaults.compaction.sessionTtlMinutes` / `idleCompactAfterMinutes` 配置入口，默认 `0` 关闭
  - `AgentLoop` 在 idle tick 与 message processing 前检查过期 session，并跳过当前活跃 session，避免后台 compact 与正在处理的 turn 抢同一 session
  - `MemoryStore.compact_idle_session` 会压缩除最近 suffix 外的旧消息，保存 `_last_summary` metadata，并通过 session manager 持久化
  - `ContextBuilder` 支持把 pending session summary 注入 runtime context，保存 turn 时仍沿用 runtime metadata 剥离逻辑，不污染 transcript
- 已覆盖测试：
  - `tests/test_autocompact.py` 覆盖 TTL/ISO timestamp、active session skip、archive delegation、failure cleanup、hot/cold summary path
  - `tests/test_autocompact.py` 覆盖 `MemoryStore.compact_idle_session` summary metadata 与 `AgentLoop` pending summary 注入
- 剩余 Runner 相关工作：
  1. 上游 `runner.py` 的 checkpoint / pending queue / microcompact 体系尚未整体拆出
  2. progress hook 仍需补 reasoning segment 与 file edit live streaming；length/empty response recovery 已有最小闭环

### 2026-05-31 / Provider factory and fallback progress

- 已完成 Slice P0/P1 的 provider factory 与 fallback 最小闭环：
  - 新增 `yuanclaw/providers/factory.py`，统一 CLI 与 Studio API 的 provider 创建路径
  - 新增 `yuanclaw/providers/fallback_provider.py`，支持 primary 失败后按 `fallbackModels` 顺序切换模型
  - `LLMResponse` 补齐 retry/fallback 所需错误元数据，并新增 `should_execute_tools` 防止错误/refusal 响应中的 tool call 被执行
  - `AgentDefaults` 新增 `fallbackModels`，新增 `ModelPresetConfig` / `InlineFallbackConfig` 和 `Config.resolve_preset`
  - `ProviderConfig` 新增 `extraBody`、`apiType`、`region`、`profile` 字段，为 Bedrock / OpenAI-compatible native port 预留
  - Provider registry/schema 补齐上游新增 provider config 入口：Bedrock、Hugging Face、Skywork、Novita、MiniMax Anthropic、StepFun、Xiaomi MIMO、LongCat、Ant Ling、LM Studio、Atomic Chat、NVIDIA NIM、Qianfan
  - `getApiBase` 支持返回 provider registry 的默认 OpenAI-compatible base URL
  - 新增 `yuanclaw/providers/openai_compatible_provider.py`，LongCat/Novita/StepFun/Xiaomi MIMO/Ant Ling、OpenRouter、VolcEngine、BytePlus 等 OpenAI-compatible provider 可绕过 LiteLLM 直接调用 OpenAI chat completions API
  - OpenAI-compatible native provider 支持 `extraBody`、extra headers、tool calls、streaming delta、reasoning effort、model prefix stripping
  - 新增 `yuanclaw/providers/bedrock_provider.py`，Bedrock 通过 native Converse / Converse Stream API 调用，不再经 LiteLLM 路由
  - Bedrock native provider 支持 `region`、`profile`、`apiBase`、bearer token、`extraBody`、tool conversion、tool result conversion、reasoning blocks、streaming delta、AWS retry/error metadata
  - `Config._match_provider` 支持 `bedrock/...` model prefix 或 `region` / `profile` 配置在无 API key 时命中 Bedrock provider
  - 新增 `yuanclaw/providers/anthropic_provider.py`，Anthropic / Claude 通过 native Anthropic Messages API 调用，不再经 LiteLLM 路由
  - Anthropic native provider 支持 API base `/v1` normalization、extra headers、OpenAI-format message/tool/tool-result/image conversion、prompt cache markers、extended thinking/adaptive thinking、tool/text/thinking stream delta、long request streaming fallback、cached token usage normalization
  - `yuanclaw.providers.__init__` 改为 lazy provider export，避免导入 package 时触发 LiteLLM 或可选 provider SDK 的副作用
- 已覆盖测试：
  - `tests/test_fallback_provider.py` 中 fallback retry、非 fallback 错误、factory wrapping、snapshot context window、provider schema/default base、fallbackModels payload、LongCat/OpenRouter/VolcEngine/BytePlus native factory、OpenAI-compatible `extraBody` / tool request、prefix stripping case、Bedrock native factory、region/profile no-key matching、Converse request build、response parsing、stream delta case
  - `tests/test_anthropic_provider.py` 中 Anthropic native factory、base URL normalization、message/tool/tool-result conversion、prompt cache marker、thinking config、Opus 4.7 temperature omission、response parse cached tokens、streaming-required fallback、text/thinking/tool stream delta case
  - 相关回归：`tests/test_commands.py`
  - 相关回归：`tests/test_studio_api.py`
- 剩余 provider 工作：
  1. Zhipu / DashScope / Moonshot / MiniMax / Gemini 等非 OpenAI-compatible 或需特殊协议/兼容性确认的 provider 仍保留 LiteLLM transitional path
  2. Anthropic native 已完成最小闭环；后续可继续迁移上游更完整的 retry/circuit-breaker/prompt-cache marker 策略
  3. model preset 暂已提供 schema/factory 基础，CLI/API 选择 preset 的外部入口后续再接
- 下一批优先级：
  1. image generation core tool/provider
  2. `apply_patch` / `exec_session` core tool runtime
  3. Core WebSocket/session replay/media route 中剩余非 UI 后端协议

### 2026-05-31 / Image generation core progress

- 已完成 Slice E 的 image generation 最小闭环：
  - 新增 `yuanclaw/agent/tools/image_generation.py`，提供 `generate_image` tool
  - `ToolsConfig` 新增 `imageGeneration` 配置，支持 `enabled`、`provider`、`model`、`defaultAspectRatio`、`defaultImageSize`、`maxImagesPerTurn`、`saveDir`
  - `AgentLoop` 根据 `tools.imageGeneration.enabled` 控制是否注册 `generate_image`
  - 新增 `yuanclaw/providers/image_generation.py`，提供 image provider 抽象、provider registry、OpenRouter image generation client 与 provider config 收集 helper
  - 新增 `yuanclaw/utils/artifacts.py`，将 data URL 图片写入 `media/generated/YYYY-MM-DD/` 并生成 sidecar metadata
  - `reference_images` 通过当前 workspace scope 校验，只允许 workspace 或 YuanClaw media 目录内图片
  - 新增内置 `image-generation` skill，并更新 `message` tool 说明，让生成后可通过 media attachment 回传
- 已覆盖测试：
  - `tests/test_image_generation_tool.py` 中 imageGeneration config、tool 注册开关、tool schema、mock provider artifact 写入、reference image 越界拒绝 case
- 剩余 image generation 工作：
  1. 目前真实 provider 只接了 OpenRouter 最小路径；AiHubMix、Gemini、Codex Responses 等上游 provider 尚未完整迁移
  2. CLI/channel 侧显式图片模式 intent prompt 尚未接入
  3. session replay / media serving API 仍需后续 Core WebSocket/API 切片补齐
- 下一批优先级：
  1. `apply_patch` / `exec_session` core tool runtime
  2. Core WebSocket/session replay/media route 中剩余非 UI 后端协议
  3. 继续补齐非 LiteLLM 原生 provider

### 2026-05-31 / Core tool runtime progress

- 已完成 Slice D 中 Agent Core tool runtime 的最小闭环：
  - 新增 `yuanclaw/agent/tools/apply_patch.py`，提供 workspace-bound structured replace/add、dry-run、multi-file summary、UTF-8 文本校验
  - 新增 `yuanclaw/agent/tools/exec_session.py`，提供长 shell session 管理、stdout/stderr 增量读取、stdin 写入、EOF、terminate、active session list、timeout cleanup、output 截断
  - `ExecTool` 新增 `yield_time_ms`、`max_output_chars` / `max_output_tokens`，可从一次 `exec` 返回 pollable `session_id`
  - `AgentLoop` 注册 `apply_patch`、`write_stdin`、`list_exec_sessions`，并为每个 AgentLoop 创建独立 `ExecSessionManager`
  - `exec` 的 `working_dir` 接入 workspace policy，one-shot 与 long session 均拒绝越界目录
  - `exec` 环境变量改为 allowlist 透传，避免把 API key / 本地 secret 暴露给模型触发的命令
  - `apply_patch` 写入前做 parent path 预检，写入异常时回滚已写文件；append 到 CRLF 文件时保留 CRLF
- 已覆盖测试：
  - `tests/test_core_runtime_tools.py` 中 apply_patch replace/add/dry-run/path boundary/atomic failure/CRLF case
  - `tests/test_core_runtime_tools.py` 中 exec session start/list/stdin/close、workspace working_dir guard、long session working_dir guard、env allowlist、AgentLoop session isolation、Studio/API session isolation case
  - 相关回归：`tests/test_filesystem_tools.py`
  - 相关回归：`tests/test_tool_validation.py`
- 剩余 tool runtime 工作：
  1. 上游更完整的 runtime_state / tool loader scopes 尚未完整迁移，目前只落了内置工具注册与 per-loop session 管理
  2. `apply_patch` 当前是 structured edit API，不是上游完整 unified diff parser；如要完全兼容 Codex 风格 patch grammar 需另开兼容切片
  3. exec session owner 已按 AgentLoop 与 current tool context 的 `channel:chat_id` 双层隔离；同一 Studio runtime 内不同 session 无法 list/poll/stdin/terminate 彼此的长 exec session
- 下一批优先级：
  1. Core WebSocket/session replay/media route 中剩余非 UI 后端协议
  2. CLI Apps / MCP presets 后端
  3. Signal channel

### 2026-05-31 / Core API session replay and media progress

- 已完成 Slice C 中 Core WebSocket/API 后端契约的第二批最小闭环：
  - `SessionManager` 新增 `read_session_file`，支持只读 replay，缺失 session 不再通过 GET 自动创建
  - 新增 `GET /api/sessions/{session_key}/messages`，返回已有 session 的 `key`、时间戳、metadata、messages
  - `GET /api/sessions/{session_key}` 改为同一只读 replay 语义，缺失返回 404
  - session replay 会把消息中的本地 `media` 路径改写为 `media_urls`，并从响应中移除 raw filesystem path
  - 新增 HMAC signed `GET /api/media/{sig}/{payload}`，只服务 `get_media_dir()` 下被签名的 media 文件
  - media route 使用 MIME allowlist，未知类型降级为 `application/octet-stream`，响应带 `X-Content-Type-Options: nosniff`；SVG 带 CSP sandbox
  - signed media route 支持单段 HTTP Range：合法范围返回 206 + `Content-Range`，非法范围返回 416 + `Content-Range: bytes */size`
  - gateway token 开启时，session replay 与 signed media route 都必须通过同一 HTTP token middleware
  - `goal_state_ws_blob` 提供 WebSocket/API 用的 bounded goal snapshot
  - `/ws/chat` 的 done frame 和 `agent.reply_done` runtime event 带 `goalState` / `goal_state`
  - `/ws/chat` turn 开始时发送 `goal_status: running` frame 并发布 `agent.goal_status` runtime event
  - `/ws/chat` turn 完成后发送 `turn_end` frame 并发布 `agent.turn_end` runtime event，携带 bounded goal snapshot
  - progress callback 支持透传 structured `tool_events` / `file_edit_events`，WebSocket frame 使用 `toolEvents` / `fileEditEvents`
- 已覆盖测试：
  - `tests/test_studio_api.py` 中 session replay metadata、signed media、raw media path stripping、tampered signature、gateway token protected media、signed media Range 206/416、missing session 404、WS goal_status / done / turn_end goal state、structured progress case
  - `tests/test_goal_state.py` 中 bounded active goal blob 与 inactive blob case
- 剩余 Core API/WebSocket 工作：
  1. 未迁移上游 append-only WebUI transcript replay；当前使用 Studio session JSONL 作为 replay 真源。本轮 Agent Core 边界下暂不引入 WebUI 专用 transcript store
  2. reconnect hydration 目前依赖 session replay 与 runtime events 最小闭环，尚未完整迁移上游 WebUI 侧 append-only hydration 协议
- 下一批优先级：
  1. CLI Apps / MCP presets 后端
  2. Signal channel
  3. 完整 CLI Apps catalog/install 与 MCP presets settings 后端

### 2026-05-31 / CLI Apps and MCP presets backend progress

- 已完成 Slice F 中 CLI Apps / MCP presets 的 Agent Core 最小闭环：
  - 新增 `yuanclaw/apps/cli/*` 与 `yuanclaw/apps/mcp_presets.py`，提供结构化 attachment normalization
  - `AgentLoop` 会从 inbound metadata 的 `cliApps` / `cli_apps`、`mcpPresets` / `mcp_presets` 规范化附件，并持久化到 session metadata
  - `ContextBuilder` 的 Runtime Context 会注入 CLI App Attachment 与 MCP Preset Attachment lines
  - user turn 持久化 `cli_apps` / `mcp_presets`，`Session.get_history()` 会为后续 turn 合成 attachment breadcrumb，避免历史上下文丢失
  - `/ws/chat` 会把 payload 中的 `cliApps` / `mcpPresets` 传入 agent runtime
  - `AgentLoop.process_direct` 支持传入 runtime attachment metadata
  - `ToolsConfig` 新增 `cliApps` 配置段，支持 `enabled`、`runTimeout`
  - 新增 `run_cli_app` tool 与 `CliAppManager` 最小实现：只运行 workspace `apps/cli/installed.json` 中声明的 installed app entry point，使用 argv subprocess、不走 shell，`working_dir` 受 workspace restriction，输出截断
  - `AgentLoop` 在 `tools.cliApps.enabled=true` 时注册 `run_cli_app`
  - 新增 `CliAppService` core service，管理 workspace `apps/cli/catalog.json` 与 `apps/cli/installed.json`
  - `CliAppService` 支持 replace/list catalog、install、settings update、uninstall、installed entry point test，并合并 installed 状态与 settings 给 catalog list
  - `MCPServerConfig` 新增 `cwd` 与 `enabledTools` 字段，stdio MCP server 会使用配置的 working directory
  - 新增 `McpPresetService` core service，基于 `Config.tools.mcp_servers` 管理内置 MCP preset catalog、enable/remove、required setting 校验、managed stdio cwd、dependency check 与 test connection
  - MCP preset payload 会 scrub secret settings，只暴露 required field configured 状态、connection summary、available/status
  - `mcp_preset_runtime_lines` 支持 configured / connected server name 集合，能区分未加载最新设置、配置存在但连接未 live、连接已 live 三种 runtime hint
- 已覆盖测试：
  - `tests/test_apps_runtime.py` 中 CLI App / MCP preset normalization、metadata persistence、runtime context injection、history breadcrumb、`process_direct` metadata、tool registration、`run_cli_app` argv execution 与 workspace cwd guard
  - `tests/test_apps_runtime.py` 中 CLI Apps core service 的 catalog/install/settings/uninstall roundtrip 与 installed entry point test
  - `tests/test_apps_runtime.py` 中 MCP presets core service 的 enable/remove、missing required secret、managed stdio cwd、missing dependency、fake MCP connection tool report、configured/connected/stale runtime lines
  - `tests/test_context_prompt_cache.py` 中 runtime context attachment lines
  - `tests/test_studio_api.py` 中 `/ws/chat` attachment payload forwarding
- 剩余 CLI Apps / MCP presets 工作：
  1. CLI Apps service 当前是 Python core service，尚未暴露 HTTP API；WebUI wiring 不在本轮范围
  2. `run_cli_app` 当前只读取本地 installed 清单；远端 registry refresh 与远端安装命令执行不在当前 slice
  3. MCP presets service 当前是 Python core service，尚未暴露 HTTP API；WebUI wiring 不在本轮范围
  4. 当前只迁移了 Agent Core 常用内置 presets 子集；完整上游 preset 列表、OAuth preset UX 和 import-cursor/custom JSON import 可后续补齐
- 下一批优先级：
  1. Signal channel
  2. CLI Apps / MCP presets HTTP API
  3. Signal SSE / typing / pairing command 完整化

### 2026-05-31 / Signal channel progress

- 已完成 Slice F 中 Signal channel 的 Agent Core 最小闭环：
  - `ChannelsConfig` 新增 `signal` 配置段，支持 `enabled`、`phoneNumber`、`daemonHost`、`daemonPort`、`attachmentsDir`、DM/group policy
  - 新增 `yuanclaw/pairing/*`，提供 pairing code 生成、approve、deny、revoke、approved sender 查询
  - 新增 `yuanclaw/channels/signal.py`，可被 channel registry / ChannelManager 自动发现
  - Signal channel 支持 signal-cli daemon JSON-RPC `send` 最小路径，出站消息带 `recipient`、plain text、attachments
  - Signal channel start 会执行 daemon `/api/v1/check` health check，并可通过 `/api/v1/events` SSE 接收 signal-cli envelope
  - SSE receive loop 支持 transient error 后 reconnect backoff，并在 channel stop 时取消后台任务
  - 支持解析 signal-cli envelope，按 DM allowlist/open/pairing approval 与 group allowlist/open policy 发布 inbound message
  - 支持 `attachmentsDir` 入站附件解析，并阻止 `../` 逃逸 attachment directory
  - 未授权 DM 可在 `dm.pairingReplyEnabled=true` 时自动回复 pairing code；默认关闭，避免未配置环境主动对外发消息
  - `yuanclaw channels pairings / approve-pairing / deny-pairing / revoke-pairing` 提供 pairing 最小 CLI 操作入口
  - 出站 markdown 先做最小 plain text 降级，避免把 `**bold**` 等标记原样发出
- 已覆盖测试：
  - `tests/test_signal_channel.py` 中 Signal config camelCase/nested policy、ChannelManager discovery、pairing approval、DM allowlist、DM deny、denied DM pairing reply、group allowlist、attachment dir boundary、JSON-RPC send、daemon health failure、SSE event parsing、transient error reconnect case
  - `tests/test_commands.py` 中 Signal pairing CLI list / approve / deny / revoke case
- 剩余 Signal 工作：
  1. 尚未迁移 typing indicator、textStyle range、长消息 split、Signal markdown table/code range 完整转换
- 下一批优先级：
  1. 完整 CLI Apps catalog/install 与 MCP presets settings 后端
  2. Signal typing / markdown style 完整化
  3. Provider native migration 与 package data/build verification

## 9. 风险与缓解

1. 风险: 上游 `0.2.0` 改动跨度大，直接复制容易引入半迁移状态。
   - 缓解: 按 slice 合并，每个 slice 都有独立测试 gate；不在一个 PR 中混入无关重构。

2. 风险: 上游 WebUI 变更和 core runtime 变更交织，迁移时误把 UI 工程带入。
   - 缓解: 只迁移 core runtime 必需的后端协议、session、media、workspace、安全能力；前端源码、UI API、打包 hook 全部排除。

3. 风险: provider 体系从 `litellm` 路径继续漂移，导致依赖和配置双轨。
   - 缓解: 迁移 provider factory 前先列出 YuanClaw 当前 provider 使用点，统一切换后再删除旧依赖。

4. 风险: 上游打包逻辑引入 Node/Bun 构建要求，影响 Python-only 开发体验。
   - 缓解: 本轮不迁移 WebUI build hook；`python -m build` 保持 Python-only。

5. 风险: 安全策略迁移不完整导致 API/media/tool 入口暴露本地文件。
   - 缓解: workspace policy 与 media route 先迁移测试，再接 UI 与 image generation。

## 10. Open Questions

- 是否在后续单独开 WebUI 同步设计文档？
- YuanClaw 是否接受 `0.2.0` 的完整 provider factory，还是只迁移 fallback/image 相关最小子集？
- Signal channel 是否进入默认发布范围，还是作为 optional extra 单独安装？
- `python-socks`、`matrix-nio` 等 Windows 条件依赖是否需要按上游一起收敛？
- `README.md` 是否在本轮同步上游多语言/官网链接结构，还是只更新本地开发文档？
