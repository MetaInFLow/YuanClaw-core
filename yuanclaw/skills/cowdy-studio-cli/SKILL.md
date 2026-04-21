---
name: cowdy-studio-cli
description: Operate Cowdy Studio through its machine interfaces with a CLI-first workflow. Use when an external agent or operator needs to inspect or drive current live Studio capabilities such as `cowdy ext`, `cowdy exec`, `cowdy cowboy`, `cowdy deploy`, backend HTTP or WebSocket routes, session and knowledge surfaces, component inventories, or Studio artifact visualization without relying on GUI clicks.
---

# Cowdy Studio CLI

Use this skill when another tool needs to operate a running Cowdy Studio through machine-callable surfaces, not through desktop interactions.

## Canonical Truth

Treat current code as the contract source of truth:

1. `packages/cowdy-cli/src/cli.mjs` defines the current CLI surface.
2. `apps/desktop/src-tauri/src/backend/mod.rs` defines the current Studio Backend HTTP and WebSocket surface.
3. `apps/desktop/src-tauri/src/backend/execution.rs` defines the current managed execution boundary.

Do not upgrade a target-state design doc into a live interface.

## Interface Priority

Always choose the narrowest stable surface that solves the task:

1. Run `cowdy ext describe`.
2. If the needed capability is present there, use `cowdy ext`.
3. Otherwise prefer the matching `cowdy` CLI command.
4. Use direct backend HTTP or WebSocket routes only when the CLI does not expose the live surface.
5. If the requested capability is `roadmap` only, say so explicitly instead of inventing a command or payload.

## Status Semantics

- `live`: safe to call as documented by current code.
- `partial`: callable, but only the listed subset is dependable.
- `roadmap`: mentioned by docs or architecture, but not a current executable contract.

## Progressive Disclosure

Do not load every reference by default.

- Start with [references/surface-map.md](references/surface-map.md) to route the request to the right surface.
- Open [references/ext-exec.md](references/ext-exec.md) when the task involves manifest-backed reads, managed execution, or execution event streams.
- Open [references/cli-commands.md](references/cli-commands.md) when you need exact `cowdy` subcommands, flags, or CLI-to-backend mapping.
- Open [references/backend-http-ws.md](references/backend-http-ws.md) only when CLI coverage is missing or when the task is explicitly about backend routes.
- Open [references/artifact-output.md](references/artifact-output.md) only when Studio should render a visualization module or artifact card after the operation completes.

## Hard Rules

- Prefer CLI over direct route calls when both are live.
- Do not treat backend routes as equivalent to `ext`; only the ext manifest defines live `ext` operations.
- Do not assume `pty`, `stdin`, terminal resize, or non-`generic-cli` managed executors are ready.
- Do not explain GUI steps such as clicking sidebars, tabs, or workbench panels.
- If a requested workflow mixes action and presentation, do the machine action first, then emit a Studio ` ```<module>` payload only for the final rendered result.

## Fast Routing Guide

- Need provider or cowboy reads: run `cowdy ext describe`, then use `cowdy ext call`.
- Need managed shell execution: use `cowdy exec`, but keep it within `generic-cli + stdio`.
- Need bundle export, install, verify, or rollback: use `cowdy cowboy` and `cowdy deploy`.
- Need inventories, config, knowledge, sessions, shared access, or Studio registry surfaces not covered by CLI: use direct backend HTTP or WebSocket routes from the references.
- Need Studio-rendered output after the action: emit the typed ` ```<module>` envelope from the artifact-output reference.
