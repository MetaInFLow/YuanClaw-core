

## YuanClaw Studio 系统架构 (Architecture v0)

### 1. 系统定位

YuanClaw Studio 是 YuanClaw Agent Core 的**控制面板与运维中心**，不是聊天客户端。用户通过 Telegram/Slack/Discord 等渠道与 Agent 对话，Studio 负责配置管理、进程管理、运行时事件流监控、会话/记忆浏览、版本管理，以及未来的用户系统。

### 2. 仓库拓扑

```
MetaInFLow/YuanClaw-core     独立仓库，Python Agent 引擎，独立版本线 (0.1.x → 1.x.x)
MetaInFLow/YuanClaw-studio   Monorepo，包含前端、Rust 壳、共享包、构建脚本
                              └─ agent/ (git submodule → YuanClaw-core)
```

Core 有自己的 CI/CD、tag、release。Studio 通过 submodule pin 某个 Core 版本，两者独立演进。

### 3. 运行时进程模型

```
┌─────────────────────────────────────────────────┐
│                  Tauri 进程                       │
│  ┌───────────────────┐  ┌────────────────────┐  │
│  │   WebView (React) │  │   Rust Shell       │  │
│  │   ─ Dashboard     │  │   ─ core_manager   │  │
│  │   ─ Config Editor │◄─┤   ─ file_access    │  │
│  │   ─ Event Stream  │  │   ─ system         │  │
│  │   ─ Sessions      │  │   ─ tray           │  │
│  │   ─ Memory        │  │                    │  │
│  │   ─ Cron          │  └────────┬───────────┘  │
│  │   ─ Logs          │           │               │
│  └───────────────────┘           │ spawn / pipe  │
└──────────────────────────────────┼───────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │     Agent Core 子进程         │
                    │  yuanclaw serve --events     │
                    │  ─ AgentLoop                 │
                    │  ─ MessageBus                │
                    │  ─ ChannelManager            │
                    │  ─ Tool Runtime              │
                    │  ─ CronService               │
                    │  ─ HeartbeatService           │
                    │  ─ EventEmitter → stdout     │
                    │  ─ 日志 → stderr              │
                    └──────────────────────────────┘
                           │            │
              ┌────────────┘            └────────────┐
              ▼                                      ▼
     外部渠道 (Telegram,                    LLM Providers
     Discord, Slack, Feishu,              (OpenRouter, OpenAI,
     QQ, Matrix, Email, WhatsApp)          Anthropic, Azure, custom)
```

整个桌面应用只有**两个进程**：Tauri 主进程（Rust + WebView）和 Agent Core 子进程（Python）。没有 FastAPI，没有 HTTP，没有 WebSocket。

### 4. 四层通信模型

| 通道 | 方向 | 机制 | 数据格式 | 用途 |
|---|---|---|---|---|
| React ↔ Rust | 双向 | Tauri IPC (`invoke` / `emit`) | JSON | 前端调用命令、接收事件推送 |
| Rust → Core | 单向 | spawn 子进程 + stdin (预留) | — | 启动/停止/信号 |
| Core → Rust | 单向 | stdout pipe | JSON Lines (每行一个事件) | 结构化事件流 |
| Core → Rust | 单向 | stderr pipe | plain text | 日志流 |
| Rust ↔ 文件系统 | 双向 | `std::fs` 直接读写 | JSON / JSONL / Markdown | 配置、会话、记忆、Cron |

**关键设计**：Rust 直接读写 `~/.yuanclaw/` 下的文件（config.json、sessions/*.jsonl、memory/*.md、cron/jobs.json），不经过 Core 进程。Core 负责运行时业务（对话、工具调用、渠道通信），Studio 负责数据展示与编辑。两者通过文件系统做隐式数据交换。

### 5. Rust Shell 模块划分

```
apps/desktop/src-tauri/src/
├─ main.rs              # Tauri 入口，注册所有 commands，管理全局 State   ~80 LOC
├─ state.rs             # AppState 定义 (CoreProcess handle, config cache)  ~30 LOC
├─ core_manager.rs      # Agent Core 子进程生命周期                          ~200 LOC
│   ├─ spawn()          #   启动 Python 进程，绑定 stdout/stderr pipe
│   ├─ stop()           #   SIGTERM → 等待 → SIGKILL
│   ├─ restart()        #   stop + spawn，可指定新版本路径
│   ├─ health_check()   #   读 stdout 心跳事件判断存活
│   ├─ stream_stdout()  #   逐行读取 stdout → window.emit("core:event", line)
│   └─ stream_stderr()  #   逐行读取 stderr → window.emit("core:log", line)
├─ file_access.rs       # ~/.yuanclaw/ 文件 CRUD                            ~150 LOC
│   ├─ read_config / write_config
│   ├─ list_sessions / read_session / delete_session
│   ├─ read_memory
│   ├─ list_cron_jobs / write_cron_jobs
│   └─ get_workspace_stats
├─ system.rs            # 系统级命令                                         ~100 LOC
│   ├─ get_core_status       #   running/stopped/error + version + uptime
│   ├─ get_studio_version
│   ├─ get_version_manifest  #   读 manifest.json
│   ├─ switch_core_version   #   切换 active worktree + restart
│   └─ set_core_connection   #   (预留) 本地/远程模式切换
└─ tray.rs              # 系统托盘：状态指示、快速操作菜单                      ~50 LOC
```

总 Rust 代码量预估 **~610 LOC**，写完后极少改动。

### 6. Agent Core 改动 (新增 EventEmitter + serve 命令)

```
yuanclaw-core/yuanclaw/
├─ events/                    # ★ 新增
│   ├─ __init__.py
│   └─ emitter.py             # ~30 LOC，EventEmitter 写 JSON Lines 到 stdout
├─ agent/
│   └─ loop.py                # 在关键节点调用 emitter.emit()，~10 行改动
├─ cli/
│   └─ commands.py            # ★ 新增 `serve` 子命令
└─ ...
```

**事件协议** (stdout JSON Lines)：

```jsonl
{"ts":"2026-03-10T12:00:01Z","type":"core.started","version":"0.1.4"}
{"ts":"...","type":"channel.message_received","channel":"telegram","from":"user123","preview":"帮我查..."}
{"ts":"...","type":"agent.thinking","session":"sess-abc"}
{"ts":"...","type":"agent.llm_chunk","session":"sess-abc","delta":"好的，"}
{"ts":"...","type":"agent.tool_call","session":"sess-abc","tool":"web_search","args":{"query":"..."}}
{"ts":"...","type":"agent.tool_result","session":"sess-abc","tool":"web_search","result":"..."}
{"ts":"...","type":"agent.reply_done","session":"sess-abc","tokens":{"input":1200,"output":350}}
{"ts":"...","type":"cron.triggered","job":"daily-summary"}
{"ts":"...","type":"heartbeat","uptime":3600,"sessions_active":2}
```

**`serve` 命令行为**：

```bash
yuanclaw serve --events              # 本地模式，Studio 用
yuanclaw serve --events --with-channels  # 同时启动渠道适配器
yuanclaw gateway                     # 原有 CLI 模式，不输出结构化事件
```

### 7. 前端架构

```
apps/desktop/src/
├─ App.tsx                    # 路由入口
├─ pages/
│   ├─ Dashboard.tsx          # 概览：Core 状态、活跃会话数、最近事件
│   ├─ EventStream.tsx        # 实时事件流（核心页面）
│   ├─ Sessions.tsx           # 会话列表 + 详情查看
│   ├─ Memory.tsx             # MEMORY.md + HISTORY.md 查看/编辑
│   ├─ Config.tsx             # config.json 可视化编辑
│   ├─ Cron.tsx               # Cron 任务管理
│   ├─ Logs.tsx               # stderr 原始日志
│   └─ Settings.tsx           # Studio 设置、Core 版本管理、(预留)用户系统
├─ components/                # 页面级组件
├─ stores/                    # Zustand stores
│   ├─ core-status.ts         # Core 运行状态
│   ├─ event-stream.ts        # 实时事件缓冲
│   └─ ...
├─ lib/
│   ├─ tauri.ts               # invoke/listen 封装
│   └─ format.ts              # 事件格式化
└─ styles/
    └─ glass-tokens.css       # Liquid glass CSS 自定义属性
```

**技术栈**：

| 关注点 | 选型 | 说明 |
|---|---|---|
| 框架 | React 19 + TypeScript | — |
| 构建 | Vite | HMR |
| 状态 | Zustand | 轻量 |
| 样式 | Tailwind CSS v4 + 自定义 CSS 变量 | 自建 liquid glass 设计系统 |
| 无障碍基座 | Radix UI primitives (headless) | 只用行为，样式自写 |
| 动效 | Framer Motion | 过渡、入场 |
| 折射效果 | 自写 SVG filter 组件 (参考 nikdelvin/liquid-glass) | 关键区域 |
| 窗口级效果 | tauri-plugin-liquid-glass (macOS 26+) | 原生增强 |
| 图标 | Lucide React | — |

### 8. 共享包

```
packages/
├─ protocol/                  # 前后端事件类型定义
│   ├─ src/
│   │   ├─ events.ts          # CoreEvent, EventType 枚举
│   │   ├─ commands.ts        # invoke 参数/返回值类型
│   │   ├─ schemas.ts         # Config, Session, CronJob 等数据结构
│   │   └─ index.ts
│   ├─ package.json
│   └─ tsconfig.json
└─ ui/                        # 自建 liquid glass 设计系统
    ├─ src/
    │   ├─ primitives/        # glass-surface, glass-refract, glass-tokens.css
    │   ├─ components/        # button, card, dialog, sidebar, input ...
    │   ├─ tailwind-plugin/   # 注册 glass-* 工具类
    │   └─ index.ts
    ├─ package.json
    └─ tsconfig.json
```

`packages/protocol` 是**前端 (TS) 与 Rust (JSON) 之间的契约**。Rust 的 `serde` 结构体应与此对齐，但因为 Rust 层基本是透传，实际只需保证 JSON 字段名一致。

### 9. 用户机器运行时目录

```
~/.yuanclaw/
├─ config.json                       # 全局配置（渠道、providers、tools）
├─ studio.json                       # (预留) Studio 专属设置（UI 偏好、连接模式）
├─ workspace/
│   ├─ sessions/
│   │   ├─ sess-abc.jsonl
│   │   └─ ...
│   ├─ memory/
│   │   ├─ MEMORY.md
│   │   └─ HISTORY.md
│   └─ cron/
│       └─ jobs.json
├─ runtime/                          # 版本管理区
│   ├─ manifest.json                 # 运行时元信息（当前版本、upstream、slot 状态）
│   ├─ active -> worktrees/stable    # 符号链接，指向当前运行版本
│   ├─ repo.git/                     # bare repo，存储所有版本的 git 对象
│   ├─ worktrees/
│   │   ├─ stable/                   # 当前运行的代码检出
│   │   └─ staging/                  # (预留) 修改中的代码检出
│   └─ envs/
│       ├─ stable/                   # 对应 venv
│       └─ staging/                  # 对应 venv
├─ hooks/                            # (预留) 外部工具事件管道
│   └─ event.pipe                    # Unix domain socket / named pipe
└─ logs/                             # (可选) 持久化日志
```

### 10. 版本管理与自演进 (预留)

**当前阶段**：客户机器上仅有 `stable` 一个 worktree，`active -> stable`，manifest 中 `local_modifications.enabled = false`。

**未来自演进流程**：

```
阶段      active     stable              staging
─────────────────────────────────────────────────────
初始       → stable   v1.3.0 (运行)        不存在
创建修改   → stable   v1.3.0 (运行)        local/evolve-001 (编辑中)
测试通过   → staging  v1.3.0 (备份/回滚)   v1.3.0+local.1 (运行)
再次修改   → staging  local/evolve-002     v1.3.0+local.1 (运行)
回滚       → stable   v1.3.0 (运行)        保留历史
上游升级   → stable   v1.4.0 (运行)        保留历史
```

两个 worktree 槽位交替使用，`active` 符号链接原子切换。Rust `core_manager` 检测到 `active` 变化后重启 Core 子进程。

对应代码占位（暂不实现）：

```
yuanclaw-core/yuanclaw/lifecycle/
├─ __init__.py          # detect_runtime_mode()
├─ version.py           # VersionInfo 数据模型
├─ manifest.py          # RuntimeManifest 读写
└─ evolution.py         # (预留) EvolutionManager: begin/test/commit/activate/rollback
```

### 11. 远程 Core 连接 (预留)

当前仅支持本地模式。预留 `studio.json` 中的配置节点：

```json
{
  "core_connection": {
    "mode": "local",
    "remote": {
      "url": "ws://192.168.1.100:8200",
      "health_url": "http://192.168.1.100:8200/health",
      "token": "..."
    }
  }
}
```

远程模式启用时，Core 需要运行 `yuanclaw serve --events --host 0.0.0.0 --port 8200`，暴露 HTTP health 端点和 WS/SSE 事件流。Rust shell 从 `core_manager` 切换到网络客户端模式。此时文件操作需走 Core 提供的 HTTP API（Core 需额外暴露 REST 端点，当前未实现）。

### 12. 外部终端 Hook (预留)

未来如果集成 Claude Code / Codex 等外部 CLI 工具，这些工具可以通过 `~/.yuanclaw/hooks/event.pipe` 写入同格式 JSON Lines，Rust shell 监听该管道并以 `external:event` 推送到前端。Core 的 EventEmitter 也可以配置同时输出到 pipe，供其他终端消费。

### 13. Monorepo 完整结构

```
YuanClaw-studio/
├─ agent/                         # git submodule → YuanClaw-core
│   ├─ yuanclaw/
│   │   ├─ cli/                   #   commands.py (含 serve)
│   │   ├─ agent/                 #   loop.py, context.py
│   │   ├─ bus/                   #   events.py, queue.py
│   │   ├─ channels/              #   manager.py, telegram.py, ...
│   │   ├─ providers/             #   base.py, litellm_provider.py, registry.py
│   │   ├─ agent/tools/           #   registry.py, *.py
│   │   ├─ config/                #   schema.py, loader.py, paths.py
│   │   ├─ session/               #   manager.py
│   │   ├─ cron/                  #   service.py
│   │   ├─ heartbeat/             #   service.py
│   │   ├─ events/                #   ★ emitter.py (新增)
│   │   ├─ lifecycle/             #   ★ version.py, manifest.py (新增)
│   │   └─ templates/
│   ├─ tests/
│   └─ pyproject.toml
│
├─ apps/
│   └─ desktop/                   # Tauri 桌面应用
│       ├─ src/                   #   React 前端
│       │   ├─ App.tsx
│       │   ├─ pages/             #     Dashboard, EventStream, Sessions, Memory,
│       │   │                     #     Config, Cron, Logs, Settings
│       │   ├─ components/
│       │   ├─ stores/            #     Zustand (core-status, event-stream, ...)
│       │   ├─ lib/               #     tauri.ts, format.ts
│       │   └─ styles/            #     glass-tokens.css
│       ├─ src-tauri/             #   Rust Shell
│       │   ├─ src/
│       │   │   ├─ main.rs
│       │   │   ├─ state.rs
│       │   │   ├─ core_manager.rs
│       │   │   ├─ file_access.rs
│       │   │   ├─ system.rs
│       │   │   └─ tray.rs
│       │   ├─ Cargo.toml
│       │   ├─ tauri.conf.json
│       │   └─ capabilities/
│       ├─ index.html
│       ├─ vite.config.ts
│       ├─ tailwind.config.ts
│       ├─ tsconfig.json
│       └─ package.json
│
├─ packages/
│   ├─ protocol/                  # TS 类型定义（事件、命令、数据结构）
│   │   ├─ src/
│   │   ├─ package.json
│   │   └─ tsconfig.json
│   └─ ui/                        # 自建 liquid glass 设计系统
│       ├─ src/
│       │   ├─ primitives/        #   glass-surface, glass-refract, tokens
│       │   ├─ components/        #   button, card, dialog, sidebar, input ...
│       │   └─ tailwind-plugin/
│       ├─ package.json
│       └─ tsconfig.json
│
├─ distribution/                  # 打包与发布
│   ├─ bundle-core.sh             #   从 submodule 打 core-vX.tar.gz
│   ├─ publish-desktop.sh         #   Tauri build + sign + notarize
│   └─ core-version.txt           #   当前 Studio 绑定的 Core 版本
│
├─ scripts/
│   ├─ bootstrap_dev_env.sh       # 开发环境初始化
│   ├─ dev.sh                     # 一键启动开发环境
│   └─ reference_repo.sh          # 外部参考仓库管理
│
├─ config/
│   └─ config.json.template       # 配置模板
│
├─ docs/
│   ├─ architecture/
│   │   └─ architecture-v0.md     # ★ 本文档
│   ├─ project/
│   │   ├─ charter-v0.md
│   │   └─ folder-declaration-v0.md
│   ├─ deployment/
│   └─ design/
│
├─ _reference_repo/               # git-ignored
├─ .gitmodules
├─ .gitignore
├─ pnpm-workspace.yaml
├─ turbo.json
├─ tsconfig.base.json
├─ package.json
└─ README.md
```

### 14. 构建与发布流水线

```
开发模式:
  pnpm dev  →  turbo 并行启动:
    1. agent/ 下 `yuanclaw serve --events` (Python)
    2. apps/desktop/ 下 `vite dev` (前端 HMR)
    3. apps/desktop/src-tauri/ 下 `cargo tauri dev` (Rust + WebView)

生产构建:
  1. distribution/bundle-core.sh  →  core-v0.1.4.tar.gz  →  apps/desktop/src-tauri/resources/
  2. pnpm build                   →  前端 dist/
  3. cargo tauri build            →  YuanClaw.app / YuanClaw.exe / YuanClaw.AppImage
     └─ 内含: Rust 二进制 + 前端 assets + resources/core-v0.1.4.tar.gz

安装首次启动:
  Tauri 检测 ~/.yuanclaw/runtime/ 不存在
  → 解压 resources/core-v0.1.4.tar.gz → ~/.yuanclaw/runtime/worktrees/stable/
  → 创建 venv → pip install → 写 manifest.json → 启动 Core
```

### 15. 质量门禁

| 层 | 命令 | 工具 |
|---|---|---|
| Python (Core) | `ruff check .` / `pytest` | ruff, pytest, pytest-asyncio |
| TypeScript (前端 + protocol + ui) | `pnpm lint` / `pnpm typecheck` / `pnpm test` | ESLint, tsc, Vitest |
| Rust | `cargo clippy` / `cargo test` | clippy, cargo test |
| 全局 | `pnpm turbo run lint typecheck test` | Turborepo |

### 16. 开发改动影响矩阵

| 场景 | Python (Core) | TypeScript (前端/包) | Rust |
|---|---|---|---|
| 新增事件类型 | emitter.emit() | protocol + UI 处理 | ❌ 不动 |
| 新增配置字段 | schema.py | protocol + Config 页面 | file_access (如涉及新文件) |
| 新增 UI 页面 | ❌ | 页面 + 路由 | ❌ 不动 |
| Core 版本切换逻辑 | lifecycle/ | Settings 页面 | system.rs |
| 系统托盘新功能 | ❌ | ❌ | tray.rs |
| 新增外部工具 Hook | (可选) | 事件处理 | hook 监听 |
| 远程模式启用 | api/ 端点 | Settings + 状态 | core_manager 切换 |

### 17. 预留能力汇总

| 能力 | 当前状态 | 触发条件 | 涉及模块 |
|---|---|---|---|
| 自演进 (staging worktree) | 目录结构预留，代码占位 | manifest 中 enabled=true | lifecycle/, core_manager |
| 远程 Core | studio.json 节点预留 | 用户配置 remote mode | core_manager, (Core api/) |
| 用户系统 | Settings 页面预留 | 业务需要时 | 前端 + (后端待定) |
| 外部终端 Hook | hooks/ 目录预留 | 集成 Claude Code/Codex 时 | Rust hook 监听, 前端事件 |
| Web 版 | packages/ui 可复用 | 业务需要时 | 新建 apps/web/ |
