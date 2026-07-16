"""Subagent workspace scope propagation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.subagent import SubagentManager
from yuanclaw.agent.tools.spawn import SpawnTool
from yuanclaw.bus.events import InboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from yuanclaw.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)


class _FakeSubagentManager:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def spawn(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "started"


@pytest.mark.asyncio
async def test_spawn_tool_passes_current_workspace_scope(tmp_path: Path) -> None:
    manager = _FakeSubagentManager()
    tool = SpawnTool(manager)  # type: ignore[arg-type]
    tool.set_context("websocket", "thread-1")
    scoped_workspace = tmp_path / "project"
    scoped_workspace.mkdir()
    scope = build_workspace_scope(scoped_workspace, "restricted", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        result = await tool.execute(task="inspect scoped files", label="inspect")
    finally:
        reset_workspace_scope(token)

    assert result == "started"
    assert manager.kwargs is not None
    assert manager.kwargs["session_key"] == "websocket:thread-1"
    assert manager.kwargs["workspace_scope"] is scope


class _SlowProvider(LLMProvider):
    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def chat(self, **kwargs: Any) -> LLMResponse:
        await asyncio.sleep(self.delay_s)
        return LLMResponse(content="subagent-ok")

    def get_default_model(self) -> str:
        return "test-model"


def test_subagent_default_max_concurrent_is_one(tmp_path: Path) -> None:
    manager = SubagentManager(
        provider=_SlowProvider(delay_s=0),
        workspace=tmp_path,
        bus=MessageBus(),
    )

    assert manager.max_concurrent_subagents == 1


def test_agent_loop_passes_max_concurrent_subagents_to_manager(tmp_path: Path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_SlowProvider(delay_s=0),
        workspace=tmp_path,
        max_concurrent_subagents=3,
        subagent_timeout_s=12.5,
    )

    assert loop.subagents.max_concurrent_subagents == 3
    assert loop.subagents.subagent_timeout_s == 12.5


@pytest.mark.asyncio
async def test_subagent_spawn_rejects_when_concurrency_limit_reached(tmp_path: Path) -> None:
    manager = SubagentManager(
        provider=_SlowProvider(delay_s=0),
        workspace=tmp_path,
        bus=MessageBus(),
        max_concurrent_subagents=1,
    )
    running = asyncio.create_task(asyncio.sleep(60))
    manager._running_tasks["sub-running"] = running
    try:
        result = await manager.spawn(task="second task", label="second")
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert "maximum concurrent subagent limit" in result.lower()
    assert "second" not in manager._running_tasks


@pytest.mark.asyncio
async def test_subagent_uses_session_wall_timeout(tmp_path: Path) -> None:
    bus = MessageBus()
    manager = SubagentManager(
        provider=_SlowProvider(delay_s=0.03),
        workspace=tmp_path,
        bus=bus,
        llm_wall_timeout_for_session=lambda _key: 0.001,
    )

    await manager._run_subagent(
        "sub-1",
        "do slow work",
        "slow",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
    )

    inbound = await bus.consume_inbound()
    assert inbound.sender_id == "subagent"
    assert "timed out after 0.001s" in inbound.content
    assert "failed" in inbound.content


@pytest.mark.asyncio
async def test_subagent_disables_wall_timeout_when_callback_returns_zero(tmp_path: Path) -> None:
    bus = MessageBus()
    manager = SubagentManager(
        provider=_SlowProvider(delay_s=0.01),
        workspace=tmp_path,
        bus=bus,
        llm_wall_timeout_for_session=lambda _key: 0.0,
    )

    await manager._run_subagent(
        "sub-1",
        "do slow work",
        "slow",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
    )

    inbound: InboundMessage = await bus.consume_inbound()
    assert "subagent-ok" in inbound.content
    assert "completed successfully" in inbound.content


@pytest.mark.asyncio
async def test_spawn_preserves_session_key_for_timeout_policy(tmp_path: Path) -> None:
    bus = MessageBus()
    observed_session_keys: list[str | None] = []
    manager = SubagentManager(
        provider=_SlowProvider(delay_s=0),
        workspace=tmp_path,
        bus=bus,
        llm_wall_timeout_for_session=lambda key: observed_session_keys.append(key) or 1.0,
    )

    await manager.spawn(task="check context", session_key="websocket:thread-7")
    await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)

    assert observed_session_keys == ["websocket:thread-7"]


@pytest.mark.asyncio
async def test_subagent_total_timeout_is_reported_as_failure(tmp_path: Path) -> None:
    bus = MessageBus()
    manager = SubagentManager(
        provider=_SlowProvider(delay_s=0.03),
        workspace=tmp_path,
        bus=bus,
        llm_wall_timeout_for_session=lambda _key: 0,
        subagent_timeout_s=0.001,
    )

    await manager._run_subagent(
        "sub-1",
        "do slow work",
        "slow",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
    )

    inbound = await bus.consume_inbound()
    assert "timed out after 0.001s" in inbound.content
    assert "failed" in inbound.content


class _ErrorProvider(LLMProvider):
    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="request failed",
            error_kind="rate_limit",
            error_status_code=429,
        )

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_subagent_rejects_structured_provider_error(tmp_path: Path) -> None:
    bus = MessageBus()
    manager = SubagentManager(provider=_ErrorProvider(), workspace=tmp_path, bus=bus)

    await manager._run_subagent(
        "sub-1",
        "do work",
        "error",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
    )

    inbound = await bus.consume_inbound()
    assert "provider returned rate_limit error (status 429)" in inbound.content
    assert "failed" in inbound.content


class _EndlessToolProvider(LLMProvider):
    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="missing", name="missing_tool", arguments={})
            ],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_subagent_iteration_exhaustion_is_reported_as_failure(tmp_path: Path) -> None:
    bus = MessageBus()
    manager = SubagentManager(
        provider=_EndlessToolProvider(),
        workspace=tmp_path,
        bus=bus,
    )

    await manager._run_subagent(
        "sub-1",
        "loop forever",
        "loop",
        {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
    )

    inbound = await bus.consume_inbound()
    assert "maximum iterations (15)" in inbound.content
    assert "failed" in inbound.content
