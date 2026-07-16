"""Tests for CLI Apps / MCP preset runtime attachment plumbing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.base import Tool
from yuanclaw.agent.tools.cli_apps import CliAppsTool
from yuanclaw.apps.cli import CliAppService
from yuanclaw.apps.mcp_presets import (
    MCP_PRESETS,
    McpPresetError,
    McpPresetService,
    mcp_preset_runtime_lines,
)
from yuanclaw.bus.events import InboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.config.schema import CliAppsToolConfig, Config
from yuanclaw.providers.base import LLMProvider, LLMResponse


class _Provider(LLMProvider):
    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        on_text_delta=None,
    ):
        return LLMResponse(content="done", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


class _FakeMcpTool(Tool):
    @property
    def name(self) -> str:
        return "mcp_playwright_browser_navigate"

    @property
    def description(self) -> str:
        return "Navigate"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "ok"


def _write_installed_app(workspace, name="echoer", entry_point="echoer"):
    app_dir = workspace / "apps" / "cli"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "installed.json").write_text(
        json.dumps(
            [
                {
                    "name": name,
                    "display_name": "Echoer",
                    "entry_point": entry_point,
                    "description": "Echo test",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_normalize_cli_app_mentions_drops_invalid_entries() -> None:
    from yuanclaw.apps.cli.utils import normalize_cli_app_mentions

    assert normalize_cli_app_mentions(
        [
            {
                "name": "Obsidian!",
                "entryPoint": "obsidian-cli",
                "displayName": "Obsidian",
            },
            {"name": "../bad", "entry_point": "bad"},
            "bad",
        ]
    ) == [
        {
            "name": "obsidian",
            "display_name": "Obsidian",
            "entry_point": "obsidian-cli",
        }
    ]


def test_cli_app_service_catalog_install_settings_uninstall_roundtrip(tmp_path) -> None:
    service = CliAppService(tmp_path)

    catalog = service.replace_catalog(
        [
            {
                "name": "Obsidian!",
                "displayName": "Obsidian",
                "entryPoint": "obsidian-cli",
                "description": "Knowledge base",
                "settings": {"vault": "~/notes"},
            },
            {"name": "../bad", "entryPoint": "bad"},
        ]
    )
    installed = service.install("obsidian", settings={"vault": "/workspace/notes"})
    listed = service.list_catalog()
    updated = service.update_settings("obsidian", {"vault": "/workspace/other"})
    removed = service.uninstall("obsidian")

    assert catalog == [
        {
            "name": "obsidian",
            "display_name": "Obsidian",
            "entry_point": "obsidian-cli",
            "description": "Knowledge base",
            "settings": {"vault": "~/notes"},
        }
    ]
    assert installed["name"] == "obsidian"
    assert installed["settings"] == {"vault": "/workspace/notes"}
    assert listed[0]["installed"] is True
    assert listed[0]["settings"] == {"vault": "/workspace/notes"}
    assert updated["settings"] == {"vault": "/workspace/other"}
    assert removed is True
    assert service.installed() == []


def test_cli_app_service_tests_installed_entry_point(tmp_path) -> None:
    service = CliAppService(tmp_path)
    service.replace_catalog([{"name": "echoer", "entryPoint": sys.executable}])
    service.install("echoer")

    ok = service.test_installed("echoer")
    missing = service.test_entry_point("missing")

    assert ok["ok"] is True
    assert ok["entry_point"] == sys.executable
    assert Path(ok["path"]).resolve() == Path(sys.executable).resolve()
    assert missing["ok"] is False


def test_normalize_mcp_preset_mentions_drops_invalid_entries() -> None:
    from yuanclaw.apps.mcp_presets import normalize_mcp_preset_mentions

    assert normalize_mcp_preset_mentions(
        [
            {
                "name": "GitHub",
                "displayName": "GitHub",
                "transport": "stdio",
            },
            {"name": "bad/name"},
            123,
        ]
    ) == [
        {
            "name": "github",
            "display_name": "GitHub",
            "transport": "stdio",
        }
    ]


def test_mcp_preset_service_catalog_enable_and_remove_roundtrip(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("yuanclaw.apps.mcp_presets.shutil.which", lambda command: f"/bin/{command}")
    config = Config()
    service = McpPresetService(config=config, runtime_root=tmp_path)

    payload = service.enable("browserbase", {"browserbase_api_key": "bb_live_secret"})
    row = next(item for item in payload["presets"] if item["name"] == "browserbase")

    assert payload["last_action"]["ok"] is True
    assert row["installed"] is True
    assert row["configured"] is True
    assert "bb_live_secret" not in str(payload)
    assert "browserbaseApiKey=bb_live_secret" in config.tools.mcp_servers["browserbase"].url

    removed = service.remove("browserbase")

    assert removed["last_action"]["ok"] is True
    assert "browserbase" not in config.tools.mcp_servers


def test_mcp_preset_service_requires_missing_secret(tmp_path) -> None:
    service = McpPresetService(config=Config(), runtime_root=tmp_path)

    with pytest.raises(McpPresetError) as exc:
        service.enable("browserbase", {})

    assert exc.value.status == 400
    assert "Browserbase API key" in exc.value.message


def test_mcp_preset_service_stdio_uses_managed_runtime_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yuanclaw.apps.mcp_presets.shutil.which", lambda command: f"/bin/{command}")
    config = Config()
    service = McpPresetService(config=config, runtime_root=tmp_path)

    payload = service.enable("playwright", {})

    row = next(item for item in payload["presets"] if item["name"] == "playwright")
    assert row["available"] is True
    assert config.tools.mcp_servers["playwright"].cwd == str(tmp_path / "mcp" / "playwright")
    assert (tmp_path / "mcp" / "playwright").is_dir()
    assert config.tools.mcp_servers["playwright"].args == [
        "-y",
        "@playwright/mcp@0.0.78",
    ]
    assert row["package_version"] == "0.0.78"


def test_builtin_npx_mcp_presets_use_pinned_versions(tmp_path) -> None:
    payload = McpPresetService(config=Config(), runtime_root=tmp_path).payload()
    npx_presets = [
        preset for preset in MCP_PRESETS if preset.server and preset.server.command == "npx"
    ]
    npx_rows = {
        row["name"]: row
        for row in payload["presets"]
        if row["name"] in {preset.name for preset in npx_presets}
    }

    assert {preset.name for preset in npx_presets} == {"playwright", "context7"}
    assert all(preset.package_version for preset in npx_presets)
    assert all("@latest" not in " ".join(preset.server.args) for preset in npx_presets)
    assert all(npx_rows[preset.name]["package_version"] for preset in npx_presets)


@pytest.mark.asyncio
async def test_mcp_preset_service_tests_missing_dependency(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("yuanclaw.apps.mcp_presets.shutil.which", lambda _command: None)
    config = Config()
    service = McpPresetService(config=config, runtime_root=tmp_path)
    service.enable("playwright", {})

    payload = await service.test("playwright")

    assert payload["last_action"]["ok"] is False
    assert "npx" in payload["last_action"]["message"]


@pytest.mark.asyncio
async def test_mcp_preset_service_tests_connection_and_reports_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("yuanclaw.apps.mcp_presets.shutil.which", lambda command: f"/bin/{command}")
    config = Config()
    service = McpPresetService(config=config, runtime_root=tmp_path)
    service.enable("playwright", {})

    async def fake_connect(servers, registry, stack):
        from yuanclaw.agent.tools.mcp import MCPConnectionResult

        assert list(servers) == ["playwright"]
        registry.register(_FakeMcpTool())
        return {
            "playwright": MCPConnectionResult(
                connected=True,
                tool_names=("mcp_playwright_browser_navigate",),
            )
        }

    monkeypatch.setattr("yuanclaw.apps.mcp_presets.connect_mcp_servers", fake_connect)

    payload = await service.test("playwright")

    assert payload["last_action"]["ok"] is True
    assert payload["last_action"]["tool_count"] == 1
    assert payload["last_action"]["tool_names"] == ["mcp_playwright_browser_navigate"]


@pytest.mark.asyncio
async def test_mcp_preset_service_reports_connection_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from yuanclaw.agent.tools.mcp import MCPConnectionResult

    monkeypatch.setattr("yuanclaw.apps.mcp_presets.shutil.which", lambda command: f"/bin/{command}")
    config = Config()
    service = McpPresetService(config=config, runtime_root=tmp_path)
    service.enable("playwright", {})

    async def fake_connect(servers, registry, stack):
        return {
            "playwright": MCPConnectionResult(
                connected=False,
                error_type="ConnectionError",
            )
        }

    monkeypatch.setattr("yuanclaw.apps.mcp_presets.connect_mcp_servers", fake_connect)

    payload = await service.test("playwright")

    assert payload["last_action"]["ok"] is False
    assert payload["last_action"]["error"] == "ConnectionError"
    assert payload["last_action"]["tool_count"] == 0


def test_mcp_runtime_lines_distinguish_configured_and_connected_state() -> None:
    metadata = {
        "mcp_presets": [
            {"name": "github", "display_name": "GitHub", "transport": "stdio"}
        ]
    }

    missing = mcp_preset_runtime_lines(metadata, configured_server_names=set(), connected_server_names=set())
    stale = mcp_preset_runtime_lines(
        metadata,
        configured_server_names={"github"},
        connected_server_names=set(),
    )
    connected = mcp_preset_runtime_lines(
        metadata,
        configured_server_names={"github"},
        connected_server_names={"github"},
    )

    assert "has not loaded the latest MCP settings" in missing[0]
    assert "connection is not currently live" in stale[0]
    assert "Prefer available tools" in connected[0]


@pytest.mark.asyncio
async def test_agent_loop_persists_app_attachments_and_injects_runtime_lines(
    tmp_path,
    monkeypatch,
) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_Provider(), workspace=tmp_path)
    captured = {}

    async def fake_run(messages, **kwargs):
        captured["content"] = messages[-1]["content"]
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop._process_message(
        InboundMessage(
            channel="studio",
            chat_id="thread-apps",
            sender_id="user",
            content="Use @obsidian and @github.",
            metadata={
                "cliApps": [
                    {
                        "name": "Obsidian",
                        "entryPoint": "obsidian-cli",
                    }
                ],
                "mcpPresets": [
                    {
                        "name": "GitHub",
                        "displayName": "GitHub",
                        "transport": "stdio",
                    }
                ],
            },
        )
    )

    session = loop.sessions.get_or_create("studio:thread-apps")
    assert session.metadata["cli_apps"] == [
        {
            "name": "obsidian",
            "entry_point": "obsidian-cli",
        }
    ]
    assert session.metadata["mcp_presets"] == [
        {
            "name": "github",
            "display_name": "GitHub",
            "transport": "stdio",
        }
    ]
    assert session.messages[0]["cli_apps"] == session.metadata["cli_apps"]
    assert session.messages[0]["mcp_presets"] == session.metadata["mcp_presets"]
    assert "CLI App Attachment: @obsidian" in captured["content"]
    assert "MCP Preset Attachment: @github" in captured["content"]


@pytest.mark.asyncio
async def test_session_history_replays_app_attachment_breadcrumbs(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_Provider(), workspace=tmp_path)

    async def fake_run(messages, **kwargs):
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop._process_message(
        InboundMessage(
            channel="studio",
            chat_id="thread-history",
            sender_id="user",
            content="First turn.",
            metadata={
                "cli_apps": [{"name": "obsidian", "entry_point": "obsidian-cli"}],
                "mcp_presets": [{"name": "github", "transport": "stdio"}],
            },
        )
    )

    history = loop.sessions.get_or_create("studio:thread-history").get_history()

    assert "CLI App Attachment: @obsidian" in history[0]["content"]
    assert "MCP Preset Attachment: @github" in history[0]["content"]
    assert "First turn." in history[0]["content"]


@pytest.mark.asyncio
async def test_process_direct_accepts_runtime_attachment_metadata(tmp_path, monkeypatch) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_Provider(), workspace=tmp_path)
    captured = {}

    async def fake_run(messages, **kwargs):
        captured["content"] = messages[-1]["content"]
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    result = await loop.process_direct(
        "Use attached app.",
        session_key="studio:thread-direct-apps",
        channel="studio",
        chat_id="thread-direct-apps",
        metadata={
            "cliApps": [{"name": "Obsidian", "entryPoint": "obsidian-cli"}],
            "mcpPresets": [{"name": "GitHub", "transport": "stdio"}],
        },
    )

    session = loop.sessions.get_or_create("studio:thread-direct-apps")
    assert result == "done"
    assert "CLI App Attachment: @obsidian" in captured["content"]
    assert "MCP Preset Attachment: @github" in captured["content"]
    assert session.metadata["cli_apps"][0]["name"] == "obsidian"
    assert session.metadata["mcp_presets"][0]["name"] == "github"


def test_agent_loop_registers_run_cli_app_when_enabled(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_Provider(),
        workspace=tmp_path,
        cli_apps_config=CliAppsToolConfig(enabled=True),
    )

    assert loop.tools.get("run_cli_app") is not None


@pytest.mark.asyncio
async def test_run_cli_app_executes_installed_entry_point_without_shell(
    tmp_path,
) -> None:
    script = tmp_path / "echoer.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    _write_installed_app(tmp_path, entry_point=sys.executable)
    tool = CliAppsTool(workspace=tmp_path)

    result = await tool.execute(
        name="echoer",
        args=[str(script), "a; echo unsafe", "b"],
    )

    assert '"a; echo unsafe"' in result
    assert "Exit code: 0" in result


@pytest.mark.asyncio
async def test_run_cli_app_rejects_working_dir_outside_restricted_workspace(tmp_path) -> None:
    _write_installed_app(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    tool = CliAppsTool(workspace=tmp_path, restrict_to_workspace=True)

    result = await tool.execute(name="echoer", working_dir=str(outside))

    assert result.startswith("Error:")
    assert "outside the configured workspace" in result
