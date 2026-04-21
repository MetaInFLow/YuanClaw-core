# Cowdy Studio Surface Map

Use this file first. It tells you which current surface to call, how stable it is, and what not to assume.

## Routing Rule

1. Probe with `cowdy ext describe`.
2. Prefer the matching `cowdy` CLI command when available.
3. Drop to direct backend HTTP or WebSocket only when CLI coverage is missing.
4. If a capability is marked `roadmap`, report it instead of fabricating a call.

## `cowdy ext`

| capability | preferred_interface | status | entrypoint | prerequisite | result_shape | fallback | do_not_assume |
|---|---|---|---|---|---|---|---|
| `providers.list` | `cowdy ext` | `live` | `cowdy ext call providers.list` | backend reachable; run `cowdy ext describe` first | JSON envelope with `data: provider[]` | `GET /api/config` when raw runtime config is required | provider mutation exists in `ext` |
| `providers.get` | `cowdy ext` | `live` | `cowdy ext call providers.get --input-json '{"id":"..."}'` | same as above; `input.id` required | JSON envelope with `data: provider` | `GET /api/config` plus local filtering | provider secrets are returned unmasked |
| `cowboys.list` | `cowdy ext` | `live` | `cowdy ext call cowboys.list` | backend reachable; run `cowdy ext describe` first | JSON envelope with `data: cowboy[]` | `GET /api/studio/cowboys` | write operations exist in `ext` |
| `cowboys.get` | `cowdy ext` | `live` | `cowdy ext call cowboys.get --input-json '{"id":"..."}'` | same as above; `input.id` required | JSON envelope with `data: cowboy` | `GET /api/studio/cowboys/:id` | apply, archive, or delete exists in `ext` |
| `notifications.watch` | `cowdy ext` | `partial` | `cowdy ext stream notifications.watch` | backend reachable; stream consumer must handle NDJSON | NDJSON stream with `stream.started`, `snapshot`, `heartbeat`, `stream.ended` | `WS /ws/events` for runtime event watching | real notification items or arbitrary subscriptions exist yet |

## `cowdy exec`

| capability | preferred_interface | status | entrypoint | prerequisite | result_shape | fallback | do_not_assume |
|---|---|---|---|---|---|---|---|
| executor profiles | `cowdy exec` | `live` | `cowdy exec profiles` | backend reachable | JSON payload with `items: ExecutorProfileRecord[]` | `GET /api/exec/profiles` | every listed profile supports managed launch |
| managed run | `cowdy exec` | `partial` | `cowdy exec run generic-cli --command "..." [--args-json '[]'] [--cwd <path>] [--env-json '{}'] [--attach]` | `generic-cli`; `command` required | `ExecutionEnvelope` with execution snapshot | `POST /api/exec/run` | non-`generic-cli`, `pty`, `stdin`, or resize are available |
| attach | `cowdy exec` | `partial` | `cowdy exec attach <execution-session-id>` | existing execution session | NDJSON replay of history plus live execution events | `GET /api/exec/:execution_session_id/attach` | interactive terminal control exists |
| inspect | `cowdy exec` | `partial` | `cowdy exec inspect <execution-session-id>` | existing execution session | `ExecutionEnvelope` with latest snapshot | `GET /api/exec/:execution_session_id` | attached output history is included in inspect |
| stop | `cowdy exec` | `partial` | `cowdy exec stop <execution-session-id>` | existing running session | `ExecutionEnvelope` with updated snapshot | `POST /api/exec/:execution_session_id/stop` | graceful process protocols beyond kill-on-stop exist |

## `cowdy cowboy` and `cowdy deploy`

| capability | preferred_interface | status | entrypoint | prerequisite | result_shape | fallback | do_not_assume |
|---|---|---|---|---|---|---|---|
| bundle export | `cowdy cowboy` | `live` | `cowdy cowboy export <cowboy-id> [--mode <single-user\|shared-isolated>] [--format <zip\|dir>] [--output <path>]` | existing cowboy id | JSON export summary | `POST /api/deploy/bundles/export` | export is an `ext` operation |
| bundle install | `cowdy cowboy` | `live` | `cowdy cowboy install <bundle-path>` | readable bundle path | JSON install summary | `POST /api/deploy/bundles/install` | install skips validation or persistence |
| installs list | `cowdy deploy` | `live` | `cowdy deploy installs` | backend reachable | JSON list of installs | `GET /api/deploy/installs` | all installs are currently healthy |
| install inspect | `cowdy deploy` | `live` | `cowdy deploy inspect <install-id>` | existing install id | JSON install record | `GET /api/deploy/installs/:install_id` | inspect also verifies |
| install verify | `cowdy deploy` | `live` | `cowdy deploy verify <install-id>` | existing install id | JSON verification result | `POST /api/deploy/installs/:install_id/verify` | verify mutates nothing beyond its own check path |
| install rollback | `cowdy deploy` | `live` | `cowdy deploy rollback <install-id>` | existing install id | JSON rollback result | `POST /api/deploy/installs/:install_id/rollback` | rollback is reversible or dry-run by default |

## Direct backend HTTP and WebSocket

| capability | preferred_interface | status | entrypoint | prerequisite | result_shape | fallback | do_not_assume |
|---|---|---|---|---|---|---|---|
| shell health | direct HTTP | `live` | `GET /health` | backend reachable | JSON health payload | none | upstream runtime health details are included |
| runtime summary | direct HTTP | `live` | `GET /api/status` | backend can reach AgentCore | JSON runtime summary | `GET /health` for backend-only probe | every Studio-native route proxies through AgentCore |
| runtime channels | direct HTTP | `live` | `GET /api/channels` | backend can reach AgentCore | JSON channel inventory | none | channel mutation is supported here |
| runtime cron inventory | direct HTTP | `live` | `GET /api/cron/jobs` | backend can reach AgentCore | JSON job list plus summary | none | cron editing is supported here |
| config read or replace | direct HTTP | `live` | `GET` or `PUT /api/config` | valid full config payload for writes | JSON config or save result | none | partial patch semantics exist |
| components scan or install | direct HTTP | `live` | `POST /api/components/scan-local`, `POST /api/components/install-local` | readable local path input | JSON scan or install report | none | a CLI wrapper exists today |
| skills inventory or install | direct HTTP | `live` | `GET /api/skills`, `POST /api/skills/install-local`, `DELETE /api/skills/:id` | readable local skill path for installs | JSON inventory or mutation result | none | skill lifecycle is exposed through `cowdy ext` |
| MCP inventory or mutate | direct HTTP | `live` | `GET/POST /api/mcp`, `PUT/DELETE /api/mcp/:id` | valid MCP manifest payload for writes | JSON inventory or mutation result | none | MCP health checks or reconnect controls are complete |
| extensions inventory or install | direct HTTP | `live` | `GET /api/extensions`, `POST /api/extensions/install-local`, `DELETE /api/extensions/:id` | readable local extension path for installs | JSON inventory or mutation result | none | extension lifecycle has a CLI wrapper |
| model pool validation | direct HTTP | `live` | `POST /api/model-pools/validate` | provider, model, modality payload | JSON validation response | none | this route persists provider config |
| shared chat or channel event injection | direct HTTP | `live` | `POST /api/shared/chat`, `POST /api/shared/channel-events` | valid shared access request | JSON chat response or channel event ack | none | this is a public SaaS ingress |
| knowledge settings and runs | direct HTTP | `live` | `GET/PUT /api/knowledge/settings`, `GET /api/knowledge/runs`, `POST /api/knowledge/runs/run-now`, `GET /api/knowledge/journals`, `GET /api/knowledge/graph` | backend reachable; valid settings payload for writes | JSON settings, run list, journal list, or graph payload | none | office-side workflows are fully closed-loop |
| Studio agent core inventory | direct HTTP | `live` | `GET /api/studio/agent-cores` | backend reachable | JSON inventory payload | none | every detected core is manageable |
| Studio cowboy registry | direct HTTP | `live` | `POST /api/studio/cowboys/bootstrap`; `GET/POST /api/studio/cowboys`; `GET/PUT/DELETE /api/studio/cowboys/:id`; `POST /api/studio/cowboys/:id/archive`; `POST /api/studio/cowboys/:id/apply` | valid cowboy payload; existing id for record-level actions | JSON registry or apply payload | use `cowdy ext` for read-only list/get | cowboy mutations are part of `ext` |
| Studio home and layout data | direct HTTP | `live` | `GET /api/studio/home`, `GET /api/studio/ui/layout-overrides`, `GET /api/team/projection` | backend reachable | JSON layout or projection payload | none | write APIs exist for layout overrides today |
| session history and summary | direct HTTP | `live` | `GET /api/sessions`, `GET /api/sessions/:session_key`, `POST /api/sessions/:session_key/summary` | existing session key for detail or summary | JSON list, detail, or summary write result | none | session deletion or arbitrary mutation exists |
| runtime chat stream | direct WebSocket | `live` | `WS /ws/chat` | websocket client that can send `chat` events | bidirectional websocket events | `POST /api/shared/chat` for shared ingress flows | `ws/chat` is the same as managed execution attach |
| runtime events stream | direct WebSocket | `live` | `WS /ws/events` | websocket client | websocket event stream | `cowdy ext stream notifications.watch` for the ext-level stream | every event has a stable product contract |

## Studio artifact rendering

| capability | preferred_interface | status | entrypoint | prerequisite | result_shape | fallback | do_not_assume |
|---|---|---|---|---|---|---|---|
| rendered visualization or artifact card | ` ```<module>` output | `live` | final assistant response only | the action already completed; a Studio component should render the result | JSON module envelope with `type` and `payload` | plain prose when no visualization is needed | module blocks are the control plane for doing the action itself |
