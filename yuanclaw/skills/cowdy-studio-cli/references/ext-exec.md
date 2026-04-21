# Cowdy Ext and Exec

Open this file only when the task needs manifest-backed reads or managed execution.

## `cowdy ext`

### Operating sequence

1. Run `cowdy ext describe`.
2. Confirm the operation or stream is actually present in the live manifest.
3. Call the manifest-backed surface.
4. If the needed capability is absent, switch to the CLI or backend fallback from the surface map.

### Current live operations

- `providers.list`
- `providers.get`
- `cowboys.list`
- `cowboys.get`

### Current stream

- `notifications.watch`
  - Current behavior is intentionally minimal.
  - Expect `stream.started`, an empty `snapshot`, periodic `heartbeat`, then `stream.ended`.

### Current command patterns

```bash
cowdy ext describe
cowdy ext call providers.list
cowdy ext call providers.get --input-json '{"id":"openai"}'
cowdy ext call cowboys.list
cowdy ext call cowboys.get --input-json '{"id":"default"}'
cowdy ext stream notifications.watch
```

### Context flags

The manifest advertises these context flags:

- `--cowboy`
- `--team`
- `--session`
- `--workspace`

Treat them as envelope-level routing context. Do not assume current provider or cowboy reads change behavior based on them unless the backend handler clearly does so.

### Error handling

- Missing `input.id` for `providers.get` or `cowboys.get` returns `invalid_request`.
- Unknown operations return `unknown_operation`.
- Missing upstream runtime config while reading providers returns `backend_unavailable`.
- Unknown stream subscriptions emit `stream.error` and then `stream.ended`.

## `cowdy exec`

Use `cowdy exec` for managed execution, but keep your expectations narrow.

### Current dependable boundary

- `generic-cli` is the only executor with managed run wiring.
- `stdio` is the only supported bridge mode.
- `pty` is explicitly rejected.
- `command` is required for `generic-cli`.
- Other agent-core executors may appear in `cowdy exec profiles`, but that does not mean managed launch is ready for them.

### Recommended sequence

```bash
cowdy exec profiles
cowdy exec run generic-cli --command "pwd" --attach
cowdy exec inspect <execution-session-id>
cowdy exec stop <execution-session-id>
```

Useful optional flags for `run`:

- `--args-json '["..."]'`
- `--env-json '{"KEY":"value"}'`
- `--cwd /absolute/path`
- `--attach`
- `--tty`
- `--bridge-mode stdio`

`--tty` is stored in the execution snapshot, but it does not unlock a PTY bridge.

### Attach stream behavior

Attach replays stored history first, then streams live execution events. Current event families include:

- `exec.state.changed`
- `exec.started`
- `exec.output.delta`
- `exec.completed`
- `exec.error`

Treat these events as current runtime behavior, not a frozen public schema.

### When not to use `cowdy exec`

- Interactive shells that need stdin
- Terminal resize handling
- PTY semantics
- Agent-core-specific managed launch beyond `generic-cli`

For those cases, report the current limitation instead of faking support.
