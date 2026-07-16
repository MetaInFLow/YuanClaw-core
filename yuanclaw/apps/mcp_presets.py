"""MCP preset catalog, settings, and runtime attachment helpers."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import urllib.parse
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from yuanclaw.agent.tools.mcp import connect_mcp_servers
from yuanclaw.agent.tools.registry import ToolRegistry
from yuanclaw.config.schema import Config, MCPServerConfig

_SAFE_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_PRESET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:[^=&]*(?:api[_-]?key|token|secret|password|bearer)[^=&]*)=)[^&#\s]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:api[_-]?key|token|secret|password|bearer)(?:[=:]|\s+))[^,\s'\"&]+",
    re.IGNORECASE,
)
_MAX_TEST_TOOLS = 16
_DEFAULT_TEST_TIMEOUT = 20


class McpPresetError(ValueError):
    """User-facing MCP preset service failure."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class McpPresetField:
    name: str
    label: str
    target: tuple[str, str]
    secret: bool = True
    required: bool = True
    env_var: str | None = None
    placeholder: str = ""


@dataclass(frozen=True)
class McpPreset:
    name: str
    display_name: str
    category: str
    description: str
    docs_url: str
    transport: str
    install_supported: bool
    brand_domain: str
    brand_color: str
    server: MCPServerConfig | None = None
    fields: tuple[McpPresetField, ...] = ()
    requires: str = ""
    note: str = ""
    package_version: str = ""


def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


MCP_PRESETS: tuple[McpPreset, ...] = (
    McpPreset(
        name="browserbase",
        display_name="Browserbase",
        category="browser",
        description="Cloud browser automation through Browserbase's hosted MCP server.",
        docs_url="https://docs.browserbase.com/integrations/mcp/setup",
        transport="streamableHttp",
        install_supported=True,
        brand_domain="browserbase.com",
        brand_color="#111827",
        requires="Browserbase API key",
        server=MCPServerConfig(
            type="streamableHttp",
            url="https://mcp.browserbase.com/mcp",
            tool_timeout=60,
        ),
        fields=(
            McpPresetField(
                name="browserbase_api_key",
                label="Browserbase API key",
                target=("url_param", "browserbaseApiKey"),
                env_var="BROWSERBASE_API_KEY",
                placeholder="bb_live_...",
            ),
        ),
    ),
    McpPreset(
        name="playwright",
        display_name="Playwright",
        category="browser",
        description="Local browser inspection and automation with Playwright's MCP server.",
        docs_url="https://playwright.dev/docs/getting-started-mcp",
        transport="stdio",
        install_supported=True,
        brand_domain="playwright.dev",
        brand_color="#2EAD33",
        requires="Node.js and npx",
        package_version="0.0.78",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@playwright/mcp@0.0.78"],
            tool_timeout=60,
        ),
    ),
    McpPreset(
        name="context7",
        display_name="Context7",
        category="docs",
        description="Fetch current library docs and code examples while the agent works.",
        docs_url="https://context7.com/docs/resources/all-clients",
        transport="stdio",
        install_supported=True,
        brand_domain="context7.com",
        brand_color="#111827",
        requires="Node.js and npx; API key optional",
        package_version="3.2.3",
        server=MCPServerConfig(
            type="stdio",
            command="npx",
            args=["-y", "@upstash/context7-mcp@3.2.3"],
            tool_timeout=45,
        ),
        fields=(
            McpPresetField(
                name="context7_api_key",
                label="Context7 API key",
                target=("arg", "--api-key"),
                env_var="CONTEXT7_API_KEY",
                placeholder="ctx7_...",
                required=False,
            ),
        ),
        note="Works without a key for basic public docs; add a key for higher limits or private docs.",
    ),
    McpPreset(
        name="github",
        display_name="GitHub",
        category="code",
        description="Repository, issue, and pull request workflows via GitHub's MCP server.",
        docs_url="https://github.com/github/github-mcp-server",
        transport="stdio",
        install_supported=True,
        brand_domain="github.com",
        brand_color="#24292F",
        requires="Docker and GitHub token",
        server=MCPServerConfig(
            type="stdio",
            command="docker",
            args=[
                "run",
                "-i",
                "--rm",
                "-e",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "ghcr.io/github/github-mcp-server",
            ],
            tool_timeout=60,
        ),
        fields=(
            McpPresetField(
                name="github_token",
                label="GitHub token",
                target=("env", "GITHUB_PERSONAL_ACCESS_TOKEN"),
                env_var="GITHUB_PERSONAL_ACCESS_TOKEN",
                placeholder="ghp_...",
            ),
        ),
    ),
    McpPreset(
        name="figma",
        display_name="Figma",
        category="design",
        description="Read design context from Figma using the local Dev Mode MCP server.",
        docs_url="https://help.figma.com/hc/en-us/articles/32132100833559-Guide-to-the-Figma-MCP-server",
        transport="streamableHttp",
        install_supported=True,
        brand_domain="figma.com",
        brand_color="#F24E1E",
        requires="Figma desktop app with MCP enabled",
        server=MCPServerConfig(
            type="streamableHttp",
            url="http://127.0.0.1:3845/mcp",
            tool_timeout=45,
        ),
        note="Requires Figma Desktop Dev Mode MCP to be running locally.",
    ),
)


def _safe_name(value: Any) -> str:
    raw = str(value or "").strip()
    if "/" in raw or "\\" in raw or ".." in raw:
        return ""
    return _SAFE_NAME_RE.sub("-", raw.lower()).strip("-")


def normalize_mcp_preset_mentions(raw: Any) -> list[dict[str, str]]:
    """Normalize structured MCP preset attachments from API/WebSocket payloads."""
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw[:8]:
        if not isinstance(item, Mapping):
            continue
        name = _safe_name(item.get("name"))
        if not name or name in seen:
            continue
        display_name = str(item.get("display_name") or item.get("displayName") or "").strip()
        transport = str(item.get("transport") or "mcp").strip() or "mcp"
        payload = {"name": name}
        if display_name:
            payload["display_name"] = display_name
        payload["transport"] = transport
        normalized.append(payload)
        seen.add(name)
    return normalized


def _preset_by_name(name: str) -> McpPreset:
    if not name or _PRESET_NAME_RE.match(name) is None:
        raise McpPresetError("invalid MCP preset name")
    for preset in MCP_PRESETS:
        if preset.name == name:
            return preset
    raise McpPresetError("unknown MCP preset", status=404)


def _clone_server(server: MCPServerConfig) -> MCPServerConfig:
    return MCPServerConfig.model_validate(server.model_dump(mode="json"))


def _url_with_param(url: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != key]
    query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _arg_value(args: list[str], flag: str) -> str | None:
    prefix = f"{flag}="
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def _with_arg_value(args: list[str], flag: str, value: str) -> list[str]:
    out: list[str] = []
    skip_next = False
    prefix = f"{flag}="
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item == flag:
            skip_next = True
            continue
        if item.startswith(prefix):
            continue
        out.append(item)
    out.extend([flag, value])
    return out


def _field_value_from_config(field: McpPresetField, cfg: MCPServerConfig | None) -> str | None:
    if cfg is None:
        return None
    target_kind, target_name = field.target
    if target_kind == "env":
        return cfg.env.get(target_name) or None
    if target_kind == "header":
        return cfg.headers.get(target_name) or None
    if target_kind == "arg":
        return _arg_value(list(cfg.args), target_name)
    if target_kind == "url_param" and cfg.url:
        parsed = urllib.parse.urlsplit(cfg.url)
        values = urllib.parse.parse_qs(parsed.query).get(target_name)
        if values:
            return values[0]
    return None


def _field_configured(field: McpPresetField, cfg: MCPServerConfig | None) -> bool:
    value = _field_value_from_config(field, cfg)
    if value:
        return True
    return bool(field.env_var and os.environ.get(field.env_var))


def _field_payload(field: McpPresetField, cfg: MCPServerConfig | None) -> dict[str, Any]:
    return {
        "name": field.name,
        "label": field.label,
        "secret": field.secret,
        "required": field.required,
        "configured": _field_configured(field, cfg),
        "placeholder": field.placeholder,
        "env_var": field.env_var,
    }


def _resolve_field_value(
    field: McpPresetField,
    settings: Mapping[str, Any],
    existing: MCPServerConfig | None,
) -> str | None:
    provided = settings.get(field.name)
    if isinstance(provided, str) and provided.strip():
        return provided.strip()
    current = _field_value_from_config(field, existing)
    if current:
        return current
    if field.env_var and os.environ.get(field.env_var):
        return f"${{{field.env_var}}}"
    return None


def _managed_cwd(runtime_root: Path, name: str) -> Path:
    return runtime_root / "mcp" / name


def _with_managed_stdio_cwd(runtime_root: Path, name: str, cfg: MCPServerConfig) -> MCPServerConfig:
    if cfg.command and (cfg.type in (None, "stdio")) and not cfg.cwd:
        cwd = _managed_cwd(runtime_root, name)
        cwd.mkdir(parents=True, exist_ok=True)
        cfg.cwd = str(cwd)
    return cfg


def _remove_managed_stdio_cwd(runtime_root: Path, name: str, cfg: MCPServerConfig | None) -> bool:
    if cfg is None or not cfg.cwd:
        return False
    cwd = Path(cfg.cwd).expanduser().resolve(strict=False)
    managed = _managed_cwd(runtime_root, name).resolve(strict=False)
    if cwd != managed or not cwd.exists():
        return False
    if cwd.is_symlink() or cwd.is_file():
        cwd.unlink()
    else:
        shutil.rmtree(cwd)
    return True


def _materialize_server(
    preset: McpPreset,
    settings: Mapping[str, Any],
    existing: MCPServerConfig | None,
    runtime_root: Path,
) -> MCPServerConfig:
    if preset.server is None or not preset.install_supported:
        raise McpPresetError(f"{preset.display_name} is not supported yet", status=409)

    cfg = _clone_server(preset.server)
    for field in preset.fields:
        value = _resolve_field_value(field, settings, existing)
        if field.required and not value:
            raise McpPresetError(f"missing {field.label}")
        if not value:
            continue
        target_kind, target_name = field.target
        if target_kind == "env":
            cfg.env[target_name] = value
        elif target_kind == "header":
            cfg.headers[target_name] = value
        elif target_kind == "arg":
            cfg.args = _with_arg_value(list(cfg.args), target_name, value)
        elif target_kind == "url_param":
            cfg.url = _url_with_param(cfg.url, target_name, value)
    return _with_managed_stdio_cwd(runtime_root, preset.name, cfg)


def _command_available(command: str) -> bool:
    if not command:
        return False
    if shutil.which(command):
        return True
    path = Path(command).expanduser()
    return path.exists() and path.is_file()


def _config_available(cfg: MCPServerConfig | None) -> bool:
    if cfg is None:
        return False
    if cfg.command:
        return _command_available(cfg.command)
    if cfg.url:
        return True
    return False


def _status_for(preset: McpPreset, cfg: MCPServerConfig | None) -> str:
    if cfg is None:
        return "not_installed" if preset.install_supported else "coming_soon"
    if any(field.required and not _field_configured(field, cfg) for field in preset.fields):
        return "missing_credentials"
    if cfg.command and not _command_available(cfg.command):
        return "missing_dependency"
    return "configured"


def _connection_summary(cfg: MCPServerConfig | None) -> str:
    if cfg is None:
        return ""
    if cfg.command:
        return " ".join([cfg.command, *cfg.args[:2]]).strip()
    if cfg.url:
        parsed = urllib.parse.urlsplit(cfg.url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return ""


def _scrub_secret_text(text: str) -> str:
    scrubbed = _SECRET_QUERY_RE.sub(r"\1<redacted>", text.strip())
    scrubbed = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", scrubbed)
    return scrubbed[:400] if scrubbed else "Connection failed."


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _test_timeout(cfg: MCPServerConfig) -> int:
    raw = cfg.tool_timeout or _DEFAULT_TEST_TIMEOUT
    return max(5, min(int(raw), _DEFAULT_TEST_TIMEOUT))


class McpPresetService:
    """Manage MCP preset settings against a YuanClaw Config object."""

    def __init__(self, *, config: Config, runtime_root: Path) -> None:
        self.config = config
        self.runtime_root = Path(runtime_root)

    def payload(
        self,
        *,
        last_action: dict[str, Any] | None = None,
        tool_preview: Mapping[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        known = {preset.name for preset in MCP_PRESETS}
        rows = [
            self._preset_payload(preset)
            | (
                {"tool_names": tool_preview[preset.name]}
                if tool_preview and preset.name in tool_preview
                else {}
            )
            for preset in MCP_PRESETS
        ]
        rows.extend(
            self._custom_payload(name, cfg, tool_names=(tool_preview or {}).get(name))
            for name, cfg in sorted(self.config.tools.mcp_servers.items())
            if name not in known
        )
        payload: dict[str, Any] = {
            "presets": rows,
            "installed_count": len(self.config.tools.mcp_servers),
        }
        if last_action is not None:
            payload["last_action"] = last_action
        return payload

    def enable(self, name: str, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
        preset = _preset_by_name(name)
        existing = self.config.tools.mcp_servers.get(preset.name)
        self.config.tools.mcp_servers[preset.name] = _materialize_server(
            preset,
            settings or {},
            existing,
            self.runtime_root,
        )
        return self.payload(
            last_action={
                "ok": True,
                "message": f"Enabled MCP preset for {preset.display_name}.",
                "installed": True,
                "verification": ["config_present"],
            }
        )

    def remove(self, name: str) -> dict[str, Any]:
        safe = _safe_name(name)
        preset = None
        try:
            preset = _preset_by_name(safe)
        except McpPresetError:
            pass
        cfg = self.config.tools.mcp_servers.get(safe)
        managed_removed = _remove_managed_stdio_cwd(self.runtime_root, safe, cfg)
        if safe in self.config.tools.mcp_servers:
            del self.config.tools.mcp_servers[safe]
        display = preset.display_name if preset else safe
        last_action: dict[str, Any] = {
            "ok": True,
            "message": f"Removed MCP preset for {display}.",
            "removed": True,
            "verification": ["config_absent"],
        }
        if managed_removed:
            last_action["managed_paths_removed"] = [f"runtime:mcp/{safe}"]
        return self.payload(last_action=last_action)

    async def test(self, name: str) -> dict[str, Any]:
        safe = _safe_name(name)
        if not safe:
            raise McpPresetError("invalid MCP server name")
        preset = None
        try:
            preset = _preset_by_name(safe)
        except McpPresetError:
            pass
        display = preset.display_name if preset else safe
        cfg = self.config.tools.mcp_servers.get(safe)
        if cfg is None:
            raise McpPresetError(f"{display} is not enabled", status=404)

        status = (
            _status_for(preset, cfg)
            if preset is not None
            else ("missing_dependency" if cfg.command and not _command_available(cfg.command) else "configured")
        )
        if status == "missing_credentials":
            return self.payload(last_action=self._test_result(False, display, "missing credentials"))
        if cfg.command and not _command_available(cfg.command):
            return self.payload(
                last_action=self._test_result(
                    False,
                    display,
                    f"{display} requires '{cfg.command}' on PATH.",
                    error="missing dependency",
                )
            )

        registry = ToolRegistry()
        async with AsyncExitStack() as stack:
            try:
                results = await asyncio.wait_for(
                    connect_mcp_servers({safe: cfg}, registry, stack),
                    timeout=_test_timeout(cfg),
                )
                result = results.get(safe)
                if result is None or not result.connected:
                    error_type = result.error_type if result is not None else "ConnectionError"
                    last_action = self._test_result(
                        False,
                        display,
                        f"{display} could not connect.",
                        error=error_type,
                    )
                    return self.payload(last_action=last_action)
                tool_prefix = f"mcp_{safe}_"
                tool_names = sorted(
                    name for name in registry.tool_names if name.startswith(tool_prefix)
                )
                last_action = {
                    "ok": True,
                    "message": (
                        f"{display} connected with {len(tool_names)} tools."
                        if tool_names
                        else f"{display} connected, but reported no tools."
                    ),
                    "tool_count": len(tool_names),
                    "tool_names": tool_names[:_MAX_TEST_TOOLS],
                    "checked_at": _checked_at(),
                }
            except asyncio.TimeoutError:
                last_action = self._test_result(False, display, f"{display} test timed out.", error="timeout")
            except Exception as exc:
                error = _scrub_secret_text(str(exc))
                last_action = self._test_result(False, display, f"{display} could not connect.", error=error)

        preview = {safe: last_action.get("tool_names", [])} if last_action.get("tool_names") else None
        return self.payload(last_action=last_action, tool_preview=preview)

    @staticmethod
    def _test_result(
        ok: bool,
        display: str,
        message: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "message": message,
            "error": error or message,
            "tool_count": 0,
            "tool_names": [],
            "checked_at": _checked_at(),
        }

    def _preset_payload(self, preset: McpPreset) -> dict[str, Any]:
        cfg = self.config.tools.mcp_servers.get(preset.name)
        status = _status_for(preset, cfg)
        configured = cfg is not None and status != "missing_credentials"
        return {
            "name": preset.name,
            "display_name": preset.display_name,
            "category": preset.category,
            "description": preset.description,
            "docs_url": preset.docs_url,
            "transport": preset.transport,
            "requires": preset.requires,
            "note": preset.note,
            "package_version": preset.package_version or None,
            "install_supported": preset.install_supported,
            "installed": cfg is not None,
            "configured": configured,
            "available": configured and _config_available(cfg),
            "status": status,
            "logo_url": _favicon_url(preset.brand_domain),
            "brand_color": preset.brand_color,
            "required_fields": [_field_payload(field, cfg) for field in preset.fields],
            "connection_summary": _connection_summary(cfg),
            "enabled_tools": list(cfg.enabled_tools) if cfg else ["*"],
            "source": "preset",
        }

    @staticmethod
    def _custom_payload(
        name: str,
        cfg: MCPServerConfig,
        *,
        tool_names: list[str] | None = None,
    ) -> dict[str, Any]:
        transport = cfg.type or (
            "stdio" if cfg.command else ("sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp")
        )
        status = "missing_dependency" if cfg.command and not _command_available(cfg.command) else "configured"
        return {
            "name": name,
            "display_name": name,
            "category": "custom",
            "description": "Custom MCP server from YuanClaw config.",
            "docs_url": "",
            "transport": transport,
            "install_supported": True,
            "installed": True,
            "configured": True,
            "available": _config_available(cfg),
            "status": status,
            "logo_url": None,
            "brand_color": "#64748B",
            "required_fields": [],
            "connection_summary": _connection_summary(cfg),
            "enabled_tools": list(cfg.enabled_tools),
            "tool_names": tool_names or [],
            "source": "custom",
        }


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    mcp_presets = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    return {"mcp_presets": mcp_presets} if isinstance(mcp_presets, list) and mcp_presets else {}


def mcp_preset_runtime_lines(
    metadata: Mapping[str, Any] | None,
    *,
    configured_server_names: set[str] | None = None,
    connected_server_names: set[str] | None = None,
) -> list[str]:
    structured = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []

    lines: list[str] = []
    for item in structured[:8]:
        if not isinstance(item, Mapping):
            continue
        name = _safe_name(item.get("name"))
        if not name:
            continue
        display = str(item.get("display_name") or name).strip() or name
        transport = str(item.get("transport") or "mcp").strip() or "mcp"
        prefix = f"mcp_{name}_"
        if configured_server_names is not None and name not in configured_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{name} ({display}; transport={transport}; tool_prefix={prefix}) "
                "has not loaded the latest MCP settings. Ask the user to restart or refresh MCP "
                "settings before relying on this integration."
            )
            continue
        if connected_server_names is not None and name not in connected_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{name} ({display}; transport={transport}; tool_prefix={prefix}) "
                "connection is not currently live. Prefer reconnecting or explaining the MCP "
                "connection state before using substitute tools."
            )
            continue
        lines.append(
            "MCP Preset Attachment: "
            f"@{name} ({display}; transport={transport}; tool_prefix={prefix}). "
            f"Prefer available tools whose names start with `{prefix}` for this request; "
            "do not substitute shell commands for this MCP integration unless the user asks."
        )
    return lines


__all__ = [
    "MCP_PRESETS",
    "McpPresetError",
    "McpPresetService",
    "mcp_preset_runtime_lines",
    "normalize_mcp_preset_mentions",
    "session_extra",
]
