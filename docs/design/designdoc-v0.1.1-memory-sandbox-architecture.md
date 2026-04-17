# Design Doc v0.1.1

## Title

YuanClaw Memory + Sandbox Architecture (inspired by OpenClaw main, reviewed on 2026-03-23)

## Goal

Design a YuanClaw-native upgrade path for:

1. memory architecture
2. SSH sandbox support
3. execution sandbox / host-exec security

The target is not a line-by-line port of OpenClaw. The target is to borrow the parts that fit YuanClaw's current Python runtime and current product shape.

## Current YuanClaw Baseline

Current YuanClaw memory and execution model is intentionally simple:

- Memory source of truth:
  - `memory/MEMORY.md`
  - `memory/HISTORY.md`
- Session persistence:
  - `sessions/*.jsonl`
  - `last_consolidated` offset per session
- Memory update path:
  - background LLM consolidation via `save_memory`
  - no semantic recall tool
  - no daily memory pages
  - no citations / snippet retrieval
  - no per-session memory scope rules
- Exec model:
  - `exec` runs local shell commands directly
  - best-effort regex safety guard only
  - no sandbox backend abstraction
  - no host approvals
  - no remote execution target

This is workable, but it has four structural limits:

1. memory retrieval quality is weak because all durable recall depends on one curated file plus grep-like history
2. compaction and memory are not token-budget-aware
3. group / non-main contexts have the same long-term memory loading behavior as private contexts
4. exec safety is policy-light and location-implicit

## What OpenClaw Is Doing Better

Based on OpenClaw docs and repo main as reviewed on 2026-03-23, the most relevant ideas are:

### Memory

- Markdown is still the source of truth
- default memory layout is layered:
  - `memory/YYYY-MM-DD.md` for daily append-only memory
  - `MEMORY.md` for curated durable memory
- memory tools are explicit:
  - `memory_search`
  - `memory_get`
- semantic memory is provided by a memory plugin slot
  - default: `memory-core`
  - optional: `memory-lancedb`
- automatic pre-compaction memory flush exists
- memory search supports:
  - hybrid search (keyword + vector)
  - citations
  - extra indexed paths
  - optional multimodal indexing
  - optional QMD sidecar backend

### Sandbox / Execution

- sandbox backend is abstracted from tool policy
- backend choices include:
  - `docker`
  - `ssh`
  - `openshell`
- sandboxing, tool allow/deny, and elevated host exec are three separate controls
- `exec` has explicit execution location and security controls:
  - `host = sandbox | gateway | node`
  - `security = deny | allowlist | full`
  - `ask = off | on-miss | always`
  - `elevated = true`
- host exec can be gated by approvals stored locally on the execution host

These ideas are worth copying. The exact TypeScript plugin machinery is not.

## Design Principles For YuanClaw

### Principle 1: Markdown stays canonical

YuanClaw should not move memory truth into SQLite, LanceDB, or a vector store.

Canonical memory should remain workspace files:

- `MEMORY.md`
- `memory/YYYY-MM-DD.md`
- optional future stable pages such as:
  - `memory/bank/entities/*.md`
  - `memory/bank/opinions.md`

Indexes are derived artifacts only.

### Principle 2: Retrieval and writing are separate concerns

Current YuanClaw mixes "memory writing" and "memory availability".

We should split them:

- capture / flush / reflect:
  - produce or update Markdown memory files
- recall:
  - return small cited snippets to the agent on demand

### Principle 3: Start with internal slots, not full plugin platform

OpenClaw uses plugin slots for memory and context engine.
YuanClaw should copy the shape first, not the whole plugin runtime.

Recommendation:

- add internal strategy interfaces:
  - `MemoryBackend`
  - `SandboxBackend`
- keep them built-in first
- later expose them as plugin slots if YuanClaw grows a general plugin system

### Principle 4: Execution location must be explicit

Current YuanClaw implicitly runs on the host.
That makes safety reasoning weak.

YuanClaw should move to:

- `sandbox` as the default exec location when sandboxing is enabled
- explicit host escape hatch
- explicit security / approval policy for host execution

## Proposed Memory Architecture

### 1. File Layout

Keep compatibility, but move new writes toward this layout:

```text
workspace/
  MEMORY.md
  memory/
    2026-03-23.md
    2026-03-24.md
    bank/
      entities/
        alice.md
        castle.md
      opinions.md
```

Compatibility rules:

- keep reading existing `memory/HISTORY.md`
- do not delete it during migration
- new auto-capture should prefer `memory/YYYY-MM-DD.md`
- `MEMORY.md` remains the durable memory anchor

### 2. Context Injection Rules

Borrow OpenClaw's scoped memory loading behavior.

Recommended rules:

- private main session:
  - load `MEMORY.md`
  - load today and yesterday daily pages
- private non-main session:
  - load `MEMORY.md`
  - do not auto-load daily pages unless session asks or memory recall injects them
- group / shared channel session:
  - do not auto-load `MEMORY.md`
  - do not auto-load daily pages
  - use `memory_search` only when needed

This reduces accidental leakage of private memory into group contexts.

### 3. Memory Backend Interface

Add a new package:

- `yuanclaw/memory/`

Core interface:

```python
class MemoryBackend(Protocol):
    def build_context(self, *, session_key: str, channel: str, is_private: bool) -> str: ...
    async def maybe_flush(self, session: Session, *, token_estimate: int) -> None: ...
    async def search(self, query: str, *, max_results: int = 8) -> list[MemoryHit]: ...
    async def get(self, path: str, *, start_line: int | None = None, end_line: int | None = None) -> MemoryDoc: ...
    async def index_if_dirty(self) -> None: ...
    async def reflect(self, *, since_days: int = 7) -> None: ...
    def status(self) -> MemoryStatus: ...
```

Initial built-ins:

- `LegacyMemoryBackend`
  - wraps today's behavior
  - keeps `MEMORY.md` + `HISTORY.md`
- `CoreMemoryBackend`
  - daily pages + SQLite FTS + optional vector layer

### 4. Memory Tools

Add agent-facing tools:

- `memory_search`
  - semantic / keyword recall
  - returns snippet + score + `path#line`
- `memory_get`
  - targeted read of a memory document or cited snippet source

Do not add a generic `memory_write` tool in phase 1.
The agent can continue using file tools for writes, while memory recall becomes structured.

Reason:

- lower API surface
- easier trust model
- avoids duplicating file-edit semantics

### 5. Automatic Memory Flush

OpenClaw's pre-compaction memory flush is a strong idea and fits YuanClaw directly.

Add:

- token estimation before compaction
- a silent "memory flush" turn when nearing budget
- prompt asks the model to persist durable notes into `MEMORY.md` or today's daily page
- flush should be skipped when workspace is not writable

Recommended config:

```json
{
  "agents": {
    "defaults": {
      "contextWindowTokens": 65536,
      "compaction": {
        "reserveTokensFloor": 12000,
        "memoryFlush": {
          "enabled": true,
          "softThresholdTokens": 4000
        }
      }
    }
  }
}
```

Trigger formula:

```text
flush when estimated_prompt_tokens >=
context_window_tokens - reserve_tokens_floor - soft_threshold_tokens
```

This should replace the current message-count-only trigger as the primary signal.

### 6. Retrieval Index

For YuanClaw, the practical order is:

#### Phase M1

- SQLite FTS5 only
- index:
  - `MEMORY.md`
  - `memory/**/*.md`
- no embeddings yet
- citations required

This already beats the current `HISTORY.md` grep model for targeted recall.

#### Phase M2

- hybrid search:
  - FTS5
  - embedding vectors in SQLite or sidecar store
- support providers:
  - OpenAI
  - Gemini
  - Ollama
  - local embedding model

#### Phase M3

- optional alternate backend slot:
  - `memory-core`
  - `memory-lancedb`
  - future `memory-qmd`

### 7. Reflection Layer

Add a scheduled reflection job, but not in phase 1.

Reflection should:

- read recent daily pages
- update entity pages
- update durable opinions/preferences
- propose durable facts for `MEMORY.md`

This is the right place for "OpenClaw-style richer memory", without polluting the hot path.

## Proposed Sandbox Architecture

### 1. New Abstraction

Add:

- `yuanclaw/sandbox/base.py`
- `yuanclaw/sandbox/local.py`
- `yuanclaw/sandbox/docker.py`
- `yuanclaw/sandbox/ssh.py`

Core interface:

```python
class SandboxBackend(Protocol):
    async def ensure_workspace(self, session_key: str, scope: str) -> SandboxWorkspace: ...
    async def exec(self, request: ExecRequest) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[DirEntry]: ...
    async def cleanup(self, *, session_key: str | None = None) -> None: ...
```

All file tools and `exec` route through this layer when sandboxing is active.

### 2. Config Shape

Recommended addition under `agents.defaults.sandbox`:

```json
{
  "agents": {
    "defaults": {
      "sandbox": {
        "mode": "off",
        "backend": "docker",
        "scope": "agent",
        "workspaceAccess": "none",
        "workspaceRoot": "~/.yuanclaw/sandboxes"
      }
    }
  }
}
```

Fields:

- `mode`: `off | non-main | all`
- `backend`: `docker | ssh`
- `scope`: `session | agent | shared`
- `workspaceAccess`: `none | ro | rw`
- `workspaceRoot`

This is intentionally close to OpenClaw, because the model is sound.

### 3. Docker Backend

This should be the first real sandbox backend for YuanClaw.

Config:

```json
{
  "docker": {
    "image": "yuanclaw-sandbox:bookworm-slim",
    "containerPrefix": "yuanclaw-sbx-",
    "workdir": "/workspace",
    "readOnlyRoot": true,
    "network": "none",
    "user": "1000:1000",
    "capDrop": ["ALL"],
    "tmpfs": ["/tmp", "/run"],
    "memory": "1g",
    "cpus": 1
  }
}
```

### 4. SSH Backend

This is the most useful OpenClaw feature to borrow after Docker.

Config:

```json
{
  "ssh": {
    "target": "user@host:22",
    "command": "ssh",
    "workspaceRoot": "/srv/yuanclaw/sandboxes",
    "identityFile": "~/.ssh/id_ed25519",
    "certificateFile": "~/.ssh/id_ed25519-cert.pub",
    "knownHostsFile": "~/.ssh/known_hosts",
    "strictHostKeyChecking": true,
    "updateHostKeys": true
  }
}
```

Behavior:

- first use:
  - create remote workspace
  - seed files into remote scope root
- after that:
  - remote workspace is canonical
  - file tools and exec run remotely
- no automatic sync-back to local workspace
- browser sandbox is not supported on SSH backend in phase 1

Implementation recommendation:

- use `asyncssh`, not `ssh` shelling-out

Reason:

- PTY support
- SFTP support
- better structured errors
- easier host key handling
- easier session reuse / connection pooling

### 5. Scope Semantics

Keep OpenClaw's scope model:

- `session`
  - strongest isolation
  - highest cost
- `agent`
  - one sandbox workspace per agent
  - best default
- `shared`
  - cheapest
  - weakest isolation

For YuanClaw, default should be:

- `mode = "non-main"`
- `scope = "agent"`
- `workspaceAccess = "none"`

That gives a safe default without breaking the user's private main workflow.

## Proposed Execution Security Model

### 1. Separate Three Concerns

Copy OpenClaw's separation:

1. sandbox decides where code runs
2. tool policy decides whether `exec` exists
3. elevated decides whether sandboxed sessions may escape to host

Current YuanClaw combines all three implicitly. That should change.

### 2. Exec Request Shape

Extend `exec` tool parameters toward:

```json
{
  "command": "...",
  "workdir": "...",
  "env": {},
  "yieldMs": 10000,
  "background": false,
  "timeout": 1800,
  "pty": false,
  "host": "sandbox",
  "security": "allowlist",
  "ask": "on-miss",
  "elevated": false
}
```

Where:

- `host = sandbox | gateway | ssh`
- `security = deny | allowlist | full`
- `ask = off | on-miss | always`

For YuanClaw, `ssh` is a better near-term third target than OpenClaw's `node`.

### 3. Approval Store

Borrow OpenClaw's local approval file idea.

Recommended file:

- `~/.yuanclaw/exec-approvals.json`

Purpose:

- host-exec allowlists
- optional prompt-on-miss policy
- safe bin profiles
- per-agent overrides

Example shape:

```json
{
  "defaults": {
    "security": "allowlist",
    "ask": "on-miss"
  },
  "allow": {
    "bins": ["git", "rg", "ls", "cat", "sed", "awk", "jq", "pytest"],
    "patterns": [
      "^git status$",
      "^git diff( .*)?$",
      "^pytest( .*)?$"
    ]
  },
  "agents": {
    "main": {
      "security": "allowlist"
    }
  }
}
```

### 4. Elevated Mode

Add session-level elevated state, but only for `exec`.

Recommended commands:

- `/elevated on`
- `/elevated off`
- `/elevated full`
- `/elevated status`

Semantics:

- `on`
  - host exec allowed, but approvals still apply
- `full`
  - host exec allowed and approval prompts are skipped
- `off`
  - sandbox only

This should only work for authorized senders.

### 5. Environment Hardening

When running host exec:

- reject `PATH` overrides
- reject `LD_*` / `DYLD_*`
- optionally reject shell startup overrides
- normalize shell selection
- cap output size
- keep background process state in memory

This is more important than regex deny-patterns.

### 6. Tool Policy Inside Sandbox

Add sandbox-specific tool allow/deny:

```json
{
  "tools": {
    "sandbox": {
      "allow": ["exec", "read_file", "list_dir", "memory_search", "memory_get"],
      "deny": ["message", "cron"]
    }
  }
}
```

This prevents "sandbox exists, but dangerous tools still operate normally" failures.

## Recommended Incremental Roadmap

### Phase A: Memory foundation

- add daily memory pages
- add `MemoryBackend` interface
- keep legacy backend default
- add `memory_search` and `memory_get`
- add SQLite FTS index
- add scoped memory loading rules

### Phase B: Token-aware compaction and flush

- add `context_window_tokens`
- replace message-count-first compaction trigger
- add silent pre-compaction memory flush

### Phase C: Docker sandbox

- add `SandboxBackend`
- route `exec` and file tools through backend
- implement Docker backend
- add sandbox tool policy

### Phase D: SSH sandbox

- implement `asyncssh` backend
- remote workspace seeding
- remote file tools and remote exec

### Phase E: Host exec approvals

- add `exec-approvals.json`
- add `host/security/ask/elevated`
- add `/elevated`

### Phase F: Rich memory backend

- add vector/hybrid retrieval
- optional `memory-lancedb`-style backend slot
- reflection jobs

## Concrete YuanClaw File Impact

### Memory

- `yuanclaw/config/schema.py`
- `yuanclaw/agent/context.py`
- `yuanclaw/agent/loop.py`
- `yuanclaw/agent/memory.py`
- `yuanclaw/command/builtin.py`
- new:
  - `yuanclaw/memory/base.py`
  - `yuanclaw/memory/core.py`
  - `yuanclaw/memory/legacy.py`
  - `yuanclaw/memory/index_sqlite.py`
  - `yuanclaw/agent/tools/memory.py`

### Sandbox / exec

- `yuanclaw/config/schema.py`
- `yuanclaw/agent/tools/shell.py`
- `yuanclaw/agent/tools/filesystem.py`
- `yuanclaw/agent/loop.py`
- new:
  - `yuanclaw/sandbox/base.py`
  - `yuanclaw/sandbox/docker.py`
  - `yuanclaw/sandbox/ssh.py`
  - `yuanclaw/sandbox/router.py`
  - `yuanclaw/security/exec_approvals.py`

## Final Recommendation

Do not jump directly to "OpenClaw-complete".

The right order for YuanClaw is:

1. adopt OpenClaw's memory file model and scoped recall behavior
2. add token-aware pre-compaction memory flush
3. add a sandbox backend abstraction
4. implement Docker first, SSH second
5. only then add host exec approvals and elevated mode
6. treat richer memory backends as a slot after the core contract is stable

If we do this in order, YuanClaw gets the important OpenClaw ideas without importing OpenClaw's full platform complexity all at once.
