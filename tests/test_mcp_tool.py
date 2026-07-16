from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.mcp import (
    MCPConnectionResult,
    MCPToolWrapper,
    connect_mcp_servers,
)
from yuanclaw.bus.queue import MessageBus
from yuanclaw.config.schema import MCPServerConfig
from yuanclaw.providers.base import LLMProvider, LLMResponse


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.fixture(autouse=True)
def _fake_mcp_module(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = ModuleType("mcp")
    mod.types = SimpleNamespace(TextContent=_FakeTextContent)
    monkeypatch.setitem(sys.modules, "mcp", mod)


def _make_wrapper(session: object, *, timeout: float = 0.1) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "test", tool_def, tool_timeout=timeout)


@pytest.mark.asyncio
async def test_execute_returns_text_blocks() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        assert arguments == {"value": 1}
        return SimpleNamespace(content=[_FakeTextContent("hello"), 42])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute(value=1)

    assert result == "hello\n42"


@pytest.mark.asyncio
async def test_execute_returns_timeout_message() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        await asyncio.sleep(1)
        return SimpleNamespace(content=[])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool), timeout=0.01)

    result = await wrapper.execute()

    assert result == "(MCP tool call timed out after 0.01s)"


@pytest.mark.asyncio
async def test_execute_handles_server_cancelled_error() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        raise asyncio.CancelledError()

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute()

    assert result == "(MCP tool call was cancelled)"


@pytest.mark.asyncio
async def test_execute_re_raises_external_cancellation() -> None:
    started = asyncio.Event()

    async def call_tool(_name: str, arguments: dict) -> object:
        started.set()
        await asyncio.sleep(60)
        return SimpleNamespace(content=[])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool), timeout=10)
    task = asyncio.create_task(wrapper.execute())
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_execute_handles_generic_exception() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        raise RuntimeError("boom")

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute()

    assert result == "(MCP tool call failed: RuntimeError)"


def test_normalizes_nullable_union_schema() -> None:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
            },
        },
    )
    wrapper = MCPToolWrapper(SimpleNamespace(call_tool=None), "test", tool_def)

    assert wrapper.parameters["properties"]["name"]["type"] == "string"
    assert wrapper.parameters["properties"]["name"]["nullable"] is True


def _install_fake_mcp_transport(
    monkeypatch: pytest.MonkeyPatch,
    behaviors: dict[str, dict],
) -> tuple[list[str], list[str]]:
    opened: list[str] = []
    closed: list[str] = []

    class StdioServerParameters:
        def __init__(self, command: str, **kwargs) -> None:
            self.command = command

    class ClientSession:
        def __init__(self, read, write) -> None:
            self.name = read

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self) -> None:
            behavior = behaviors[self.name]
            if delay := behavior.get("initialize_delay"):
                await asyncio.sleep(delay)
            if error := behavior.get("initialize_error"):
                raise error

        async def list_tools(self):
            behavior = behaviors[self.name]
            if delay := behavior.get("discovery_delay"):
                await asyncio.sleep(delay)
            tools = [
                SimpleNamespace(
                    name=name,
                    description=name,
                    inputSchema={"type": "object", "properties": {}},
                )
                for name in behavior.get("tools", [])
            ]
            return SimpleNamespace(tools=tools)

    @asynccontextmanager
    async def stdio_client(params):
        opened.append(params.command)
        try:
            yield params.command, object()
        finally:
            closed.append(params.command)

    @asynccontextmanager
    async def unused_transport(*args, **kwargs):
        raise AssertionError("unexpected transport")
        yield

    mcp = sys.modules["mcp"]
    mcp.ClientSession = ClientSession
    mcp.StdioServerParameters = StdioServerParameters
    client = ModuleType("mcp.client")
    stdio = ModuleType("mcp.client.stdio")
    stdio.stdio_client = stdio_client
    sse = ModuleType("mcp.client.sse")
    sse.sse_client = unused_transport
    streamable = ModuleType("mcp.client.streamable_http")
    streamable.streamable_http_client = unused_transport
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", sse)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", streamable)
    return opened, closed


@pytest.mark.asyncio
async def test_connect_mcp_servers_isolates_failures_and_filters_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened, closed = _install_fake_mcp_transport(
        monkeypatch,
        {
            "good": {"tools": ["allowed", "hidden"]},
            "bad": {"initialize_error": RuntimeError("unavailable")},
        },
    )
    from yuanclaw.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    configs = {
        "good": MCPServerConfig(
            command="good",
            enabled_tools=["allowed"],
        ),
        "bad": MCPServerConfig(command="bad"),
    }

    async with AsyncExitStack() as stack:
        results = await connect_mcp_servers(configs, registry, stack)
        assert opened == ["good", "bad"]
        assert closed == ["bad"]
        assert results["good"].connected is True
        assert results["good"].tool_names == ("mcp_good_allowed",)
        assert results["bad"].connected is False
        assert results["bad"].error_type == "RuntimeError"
        assert registry.tool_names == ["mcp_good_allowed"]

    assert closed == ["bad", "good"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "timeout_field"),
    [
        ({"initialize_delay": 0.03}, "connect_timeout"),
        ({"discovery_delay": 0.03}, "discovery_timeout"),
    ],
)
async def test_connect_mcp_servers_bounds_connection_phases(
    monkeypatch: pytest.MonkeyPatch,
    behavior: dict,
    timeout_field: str,
) -> None:
    _install_fake_mcp_transport(monkeypatch, {"slow": behavior})
    from yuanclaw.agent.tools.registry import ToolRegistry

    config_kwargs = {timeout_field: 0.001}
    config = MCPServerConfig(command="slow", **config_kwargs)
    async with AsyncExitStack() as stack:
        results = await connect_mcp_servers(
            {"slow": config},
            ToolRegistry(),
            stack,
        )

    assert results["slow"] == MCPConnectionResult(
        connected=False,
        error_type="TimeoutError",
    )


class _LoopProvider(LLMProvider):
    async def chat(self, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_agent_loop_retries_failed_mcp_server_and_unregisters_on_close(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def fake_connect(servers, registry, stack):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {
                "demo": MCPConnectionResult(
                    connected=False,
                    error_type="ConnectionError",
                )
            }
        registry.register(SimpleNamespace(name="mcp_demo_ping"))
        return {
            "demo": MCPConnectionResult(
                connected=True,
                tool_names=("mcp_demo_ping",),
            )
        }

    monkeypatch.setattr("yuanclaw.agent.tools.mcp.connect_mcp_servers", fake_connect)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_LoopProvider(),
        workspace=tmp_path,
        mcp_servers={"demo": MCPServerConfig(command="demo")},
    )

    await loop._connect_mcp()
    assert loop._mcp_connected is False
    await loop._connect_mcp()
    assert loop._mcp_connected is True
    assert attempts == 2
    assert loop.tools.has("mcp_demo_ping")

    await loop.close_mcp()
    assert loop._mcp_connected is False
    assert not loop.tools.has("mcp_demo_ping")
