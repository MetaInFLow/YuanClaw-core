# YuanClaw Core — Studio v0.1.0 开发 Delta 设计文档

- 文档版本: `v0.1.0`
- 日期: `2026-03-10`
- 目标分支: `dev`
- 适用仓库: `MetaInFLow/YuanClaw-core`
- 对齐文档: `docs/engineering/specs/designdoc-v0.1.0-whole-system-overview.md`（Studio 根目录）

## 1. 背景

Studio v0.1.0 目标架构已明确为：
- 本地模式采用 `Tauri Rust Shell <-> Core 子进程` 的 `stdin/stdout/stderr` 管道通信
- Core 新增 `serve` 入口，输出结构化事件流（stdout）并接收命令（stdin）
- `gateway` 保持兼容，不破坏现有渠道运行能力

当前 `dev` 分支 Core 代码具备稳定的 AgentLoop、ChannelManager、CronService、Session/Memory 等基础能力，但尚未提供 Studio 所需的进程通信契约。

## 2. 范围与非目标

### 2.1 本次范围（必须在 v0.1.0 完成）

- 新增 `yuanclaw serve` 命令（本地托管入口）
- 新增 `events/` 模块（EventEmitter + 类型/协议）
- 增加 stdout/stderr 输出路由隔离（OutputRouter）
- 增加 stdin 命令监听与分发（StdinListener）
- 在 Agent/Channel/Cron 关键路径增加事件埋点
- 增加最小 `lifecycle/` 占位接口（只读 + NotImplementedError 写接口）
- 补齐测试护栏（命令、事件、路由、契约）

### 2.2 非目标（v0.1.0 不做）

- 远程模式网络接口（HTTP/WS）正式实现
- 自演进完整流程（staging worktree 修改/测试/切换）
- 用户系统与认证体系

## 3. 当前实现基线（dev 分支）

基线 commit: `4c4e247`（`origin/dev`）

### 3.1 已有能力

- CLI 已有 `gateway` 命令与 `agent` 命令，`gateway` 可运行 AgentLoop + ChannelManager + CronService
- 配置路径工具已提供：`get_data_dir()`、`get_runtime_subdir()`、`get_workspace_path()`、`get_media_dir()`、`get_cron_dir()`、`get_logs_dir()`、`get_bridge_install_dir()`、`get_legacy_sessions_dir()`
- Core 具备多渠道适配、工具执行、会话持久化、Cron、Heartbeat 能力

### 3.2 缺失能力（与 Studio v0.1.0 目标相比）

- 无 `serve` 命令
- 无 `events/` 模块、无统一事件枚举/schema
- 无 stdout/stderr 强制分离机制
- 无 stdin 控制命令协议
- 无 `lifecycle/` 占位模块
- 无面向 Studio 协议的一组测试

## 4. Delta 需求清单（按优先级）

### 4.1 P0（首批必做）

1. `serve` 命令
- 文件: `yuanclaw/cli/commands.py`
- 新增 `@app.command() def serve(...)`
- 支持参数：`--events/--no-events`、`--with-channels`
- 启动顺序：OutputRouter -> init_emitter -> initialize_runtime -> StdinListener -> emit `core.started`
- 关闭顺序：emit `core.stopping` -> 关闭服务

2. 事件模块
- 新增目录: `yuanclaw/events/`
- 文件: `emitter.py`、`types.py`、`protocol.py`、`__init__.py`
- 要求：
  - `emit()` 调用方永不阻塞
  - 使用有界缓冲（如 `deque(maxlen=...)`）+ 独立 drain 线程写 stdout
  - 事件至少包含 `ts/type/priority`

3. 事件埋点
- `yuanclaw/agent/loop.py`: `agent.thinking` / `agent.llm_chunk` / `agent.tool_call` / `agent.tool_result` / `agent.reply_done` / `agent.error`
- `yuanclaw/channels/manager.py`: `channel.message_received` / `channel.reply_sent`
- `yuanclaw/cron/service.py`: `cron.triggered` / `cron.updated`

4. stdin 命令通道
- 新增监听线程（可放 `serve` 相关模块）
- 支持命令：`config.reload`、`cron.add`、`cron.remove`、`file.lock_request`、`file.unlock`
- 响应通过 stdout 事件返回：`config.reloaded`、`cron.updated`、`file.lock_granted`、`file.unlocked`

### 4.2 P1（稳定性与契约补齐）

1. OutputRouter（stdout/stderr 分离）
- `serve` 模式：stdout 仅结构化事件；stderr 仅日志/控制台输出
- `gateway` 模式：保持当前行为，避免回归风险

2. 文件写入原子化与写入方约束
- `cron/jobs.json`、`config.json` 采用 temp + rename
- 明确写入责任：
  - `sessions/*.jsonl` Core append-only
  - `cron/jobs.json` Core 独占写
  - `MEMORY.md` 在锁协议下允许 Studio 写

3. 路径契约增强
- `yuanclaw/config/paths.py` 增补并统一命名：
  - `get_runtime_dir()`
  - `get_hooks_dir()`
  - `get_logs_dir()`（与现有实现对齐）
- 与文档路径保持一致（`~/.yuanclaw/cron/jobs.json` 等）

### 4.3 P2（预留骨架）

1. lifecycle 占位
- 新增目录: `yuanclaw/lifecycle/`
- 文件: `version.py`、`manifest.py`、`evolution.py`、`__init__.py`
- 要求：
  - `VersionInfo`、`RuntimeManifest` 只读能力可用
  - 写接口先 `raise NotImplementedError`

2. 远程模式接口占位
- 在 `serve` 参数中保留 `--host/--port/--allow-external/--token`（可先不生效）
- 明确私网限制与 token 约束

## 5. 代码改动映射

| 类型 | 路径 | 动作 |
|---|---|---|
| 修改 | `yuanclaw/cli/commands.py` | 新增 `serve`，拆分共享初始化逻辑 |
| 新增 | `yuanclaw/events/` | EventEmitter、事件类型、协议工具 |
| 修改 | `yuanclaw/agent/loop.py` | 增加关键节点 emit |
| 修改 | `yuanclaw/channels/manager.py` | 增加收发消息 emit |
| 修改 | `yuanclaw/cron/service.py` | 增加触发/更新事件 emit；写入原子化 |
| 新增 | `yuanclaw/lifecycle/` | 版本/manifest/evolution 占位骨架 |
| 修改 | `yuanclaw/config/paths.py` | 路径契约函数补齐 |
| 新增 | `tests/test_serve_command.py` 等 | Studio 契约测试 |

## 6. 里程碑（Core dev 侧）

1. M0（P0 完成）
- `serve` + `events/` + 基础埋点 + stdin 命令链路可跑通

2. M1（P1 完成）
- 输出路由稳定、文件写入契约落地、路径契约统一

3. M2（P2 完成）
- `lifecycle/` 占位与远程模式参数占位完成

## 7. 测试与验收标准

- 命令级
  - `yuanclaw serve --events` 可启动并输出 `core.started`
  - `SIGTERM` 后输出 `core.stopping` 并优雅退出

- 协议级
  - stdout 每行 JSON 可解析，必含 `ts/type/priority`
  - stderr 不包含结构化事件

- 兼容级
  - `yuanclaw gateway` 行为不变
  - `yuanclaw agent` 行为不变

- 可靠性
  - 高频 `agent.llm_chunk` 不阻塞 Agent 主流程
  - stdin 命令异常不会导致主循环崩溃

## 8. 风险与缓解

1. 风险：事件流过载导致卡顿
- 缓解：有界队列 + 丢弃 `normal` 事件策略 + chunk 合并

2. 风险：输出路由改造影响现有 CLI
- 缓解：只在 `serve` 启用 OutputRouter，`gateway` 保持原样

3. 风险：文件锁协议不完整导致 memory 覆盖
- 缓解：先实现最小锁协议 + 超时与崩溃兜底策略

## 9. 开放问题

已决策（从 open questions 关闭）：
- `serve` 与 `gateway` 共享 `initialize_runtime()`，以减少双实现分叉风险
- `agent.llm_chunk` 采样与合并放 Rust 侧（50ms 窗口），Core 保持完整事件输出

待决策：
1. `file.lock_request` 是否需要返回租约 TTL 与 owner 信息？
2. `cron.add/remove` 是否在 v0.1.0 只支持最小字段集（id/schedule/payload）？
