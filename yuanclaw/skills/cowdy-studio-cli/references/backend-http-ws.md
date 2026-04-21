# Cowdy Studio Backend HTTP and WebSocket

Open this file only when a live Studio surface is not covered by `cowdy ext` or another `cowdy` CLI command.

## Transport rules

- Use the same backend base URL the CLI would use.
- Default backend URL is `http://127.0.0.1:18616` unless the environment overrides it.
- Prefer JSON HTTP routes for request or response style operations.
- Prefer NDJSON or WebSocket only for live stream use cases.

## Direct route families

### Probe and upstream runtime passthrough

- `GET /health`
  - Studio Backend shell health only.
- `GET /api/status`
  - runtime summary passthrough
- `GET /api/channels`
  - runtime channels passthrough
- `GET /api/cron/jobs`
  - runtime cron summary passthrough

Use these for reachability or runtime visibility. Do not treat them as a replacement for `ext`.

### Studio-native configuration and inventories

- `GET/PUT /api/config`
- `POST /api/components/scan-local`
- `POST /api/components/install-local`
- `GET /api/skills`
- `POST /api/skills/install-local`
- `DELETE /api/skills/:id`
- `GET/POST /api/mcp`
- `PUT/DELETE /api/mcp/:id`
- `GET /api/extensions`
- `POST /api/extensions/install-local`
- `DELETE /api/extensions/:id`
- `POST /api/model-pools/validate`

Use these when the task is explicitly about local Studio inventory, installation, or validation.

### Knowledge and shared access

- `GET/PUT /api/knowledge/settings`
- `GET /api/knowledge/runs`
- `POST /api/knowledge/runs/run-now`
- `GET /api/knowledge/journals`
- `GET /api/knowledge/graph`
- `POST /api/shared/chat`
- `POST /api/shared/channel-events`

Use these when the task is about knowledge automation state, shared ingress, or channel event injection.

### Studio registry and team surfaces

- `GET /api/studio/agent-cores`
- `POST /api/studio/cowboys/bootstrap`
- `GET/POST /api/studio/cowboys`
- `GET/PUT/DELETE /api/studio/cowboys/:id`
- `POST /api/studio/cowboys/:id/archive`
- `POST /api/studio/cowboys/:id/apply`
- `GET /api/studio/home`
- `GET /api/studio/ui/layout-overrides`
- `GET /api/team/projection`

Use these for Studio-native registry or workspace projection tasks. For read-only cowboy lookups, prefer `cowdy ext` first.

### Sessions and event transports

- `GET /api/sessions`
- `GET /api/sessions/:session_key`
- `POST /api/sessions/:session_key/summary`
- `WS /ws/chat`
- `WS /ws/events`

Use session HTTP routes for history and summary tasks. Use WebSocket only for live runtime chat or event watching.

## Fallback policy

- If a task fits `cowdy ext`, go back to `ext`.
- If a task fits an existing `cowdy` CLI command, go back to the CLI.
- Use direct backend routes only when they are the first live surface for the requested capability.
- If you must explain a gap, name the missing live surface and stop there. Do not invent placeholder endpoints.
