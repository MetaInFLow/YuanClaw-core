# Cowdy CLI Commands

Open this file when you need exact `cowdy` syntax or a direct mapping from CLI to backend routes.

## Base URL and transport

- CLI reads `COWDY_BACKEND_URL` when set.
- Otherwise it uses the default Studio Backend URL: `http://127.0.0.1:18616`.
- `ext` and `deploy` commands use JSON over HTTP.
- `ext stream` and `exec attach` use NDJSON streaming responses.

## Top-level commands

| CLI command | backend entrypoint | purpose |
|---|---|---|
| `cowdy ext describe` | `GET /api/ext/describe` | read the live ext manifest |
| `cowdy ext call <operation> [--input-json '{...}'] [--context-json '{...}']` | `POST /api/ext/call` | invoke one ext operation |
| `cowdy ext stream <subscription> [--input-json '{...}'] [--context-json '{...}']` | `POST /api/ext/stream/:subscription` | attach to one ext stream |
| `cowdy exec profiles` | `GET /api/exec/profiles` | list executor profiles |
| `cowdy exec run <executor-ref> ...` | `POST /api/exec/run` | start managed execution |
| `cowdy exec attach <execution-session-id>` | `GET /api/exec/:execution_session_id/attach` | replay and tail execution events |
| `cowdy exec inspect <execution-session-id>` | `GET /api/exec/:execution_session_id` | inspect execution snapshot |
| `cowdy exec stop <execution-session-id>` | `POST /api/exec/:execution_session_id/stop` | request stop |
| `cowdy cowboy export <cowboy-id> ...` | `POST /api/deploy/bundles/export` | export one cowboy bundle |
| `cowdy cowboy install <bundle-path>` | `POST /api/deploy/bundles/install` | install one exported bundle |
| `cowdy deploy installs` | `GET /api/deploy/installs` | list installs |
| `cowdy deploy inspect <install-id>` | `GET /api/deploy/installs/:install_id` | inspect one install |
| `cowdy deploy verify <install-id>` | `POST /api/deploy/installs/:install_id/verify` | verify one install |
| `cowdy deploy rollback <install-id>` | `POST /api/deploy/installs/:install_id/rollback` | roll back one install |

## Recommended operator flows

### Read manifest-backed data

```bash
cowdy ext describe
cowdy ext call providers.list
cowdy ext call cowboys.get --input-json '{"id":"default"}'
```

### Start a managed shell command

```bash
cowdy exec profiles
cowdy exec run generic-cli --command "ls" --args-json '["-la"]' --cwd /tmp --attach
```

### Export and install a cowboy bundle

```bash
cowdy cowboy export sales-agent --mode single-user --format zip
cowdy cowboy install /tmp/sales-agent.zip
```

### Inspect, verify, or roll back an install

```bash
cowdy deploy installs
cowdy deploy inspect install_123
cowdy deploy verify install_123
cowdy deploy rollback install_123
```

## Selection rules

- Use `ext` only for operations present in `cowdy ext describe`.
- Use `exec` only when `generic-cli + stdio` is enough.
- Use `cowdy cowboy` and `cowdy deploy` for deployment lifecycle tasks, not `ext`.
- If a current live backend route has no CLI wrapper, switch to the backend HTTP or WebSocket reference instead of inventing a new `cowdy` subcommand.
