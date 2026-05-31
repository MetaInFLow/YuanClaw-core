"""Tests for sustained-goal continuation inside AgentLoop."""

from __future__ import annotations

import asyncio

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMProvider, LLMResponse
from yuanclaw.session.goal_state import GOAL_STATE_KEY


class _PlainProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, on_text_delta=None):
        self.calls += 1
        return LLMResponse(content="still working", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


class _SlowProvider(LLMProvider):
    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, on_text_delta=None):
        await asyncio.sleep(self.delay_s)
        return LLMResponse(content="slow-ok", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


class _HistoryRecordingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.first_call_entered = asyncio.Event()
        self.first_call_can_finish = asyncio.Event()
        self.seen_user_counts: list[int] = []
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, on_text_delta=None):
        self.calls += 1
        call_number = self.calls
        self.seen_user_counts.append(
            sum(1 for message in messages if message.get("role") == "user")
        )
        if call_number == 1:
            self.first_call_entered.set()
            await self.first_call_can_finish.wait()
        return LLMResponse(content=f"reply-{call_number}", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, on_text_delta=None):
        self.calls.append([dict(message) for message in messages])
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_run_agent_loop_continues_plain_text_when_goal_active(tmp_path) -> None:
    provider = _PlainProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        max_iterations=3,
    )

    final_content, _, messages, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "do it"}],
        goal_active_predicate=lambda: True,
    )

    assert provider.calls == 3
    assert "maximum number of tool call iterations" in final_content
    assert any(
        m.get("role") == "user" and "active sustained goal" in str(m.get("content", ""))
        for m in messages
    )


@pytest.mark.asyncio
async def test_run_agent_loop_exits_plain_text_when_goal_inactive(tmp_path) -> None:
    provider = _PlainProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    final_content, _, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "do it"}],
        goal_active_predicate=lambda: False,
    )

    assert provider.calls == 1
    assert final_content == "still working"


@pytest.mark.asyncio
async def test_process_message_passes_session_goal_state_to_loop(tmp_path, monkeypatch) -> None:
    provider = _PlainProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.metadata[GOAL_STATE_KEY] = {"status": "active", "objective": "Keep going."}

    captured = {}

    async def fake_run(messages, **kwargs):
        captured["goal_active"] = kwargs["goal_active_predicate"]()
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    from yuanclaw.bus.events import InboundMessage

    await loop._process_message(
        InboundMessage(channel="cli", chat_id="direct", sender_id="u", content="continue")
    )

    assert captured["goal_active"] is True


@pytest.mark.asyncio
async def test_active_goal_disables_runner_wall_llm_timeout_for_process_direct(tmp_path) -> None:
    provider = _SlowProvider(delay_s=0.03)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        llm_timeout_s=0.001,
    )
    session = loop.sessions.get_or_create("cli:direct")
    session.metadata[GOAL_STATE_KEY] = {"status": "active", "objective": "Keep going."}
    loop.sessions.save(session)
    loop.max_iterations = 1

    result = await loop.process_direct("continue")

    assert result == "I reached the maximum number of tool call iterations (1) without completing the task. You can try breaking the task into smaller steps."


@pytest.mark.asyncio
async def test_inactive_goal_uses_runner_wall_llm_timeout_for_process_direct(tmp_path) -> None:
    provider = _SlowProvider(delay_s=0.03)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        llm_timeout_s=0.001,
    )

    result = await loop.process_direct("continue")

    assert "timed out after 0.001s" in result


@pytest.mark.asyncio
async def test_process_direct_serializes_same_session_turns_before_building_history(tmp_path) -> None:
    provider = _HistoryRecordingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
    )

    first = asyncio.create_task(loop.process_direct("first", session_key="cli:direct"))
    await provider.first_call_entered.wait()
    second = asyncio.create_task(loop.process_direct("second", session_key="cli:direct"))
    await asyncio.sleep(0)

    provider.first_call_can_finish.set()
    await asyncio.gather(first, second)

    reloaded = type(loop.sessions)(tmp_path).get_or_create("cli:direct")
    persisted = [
        (message["role"], message["content"])
        for message in reloaded.messages
        if message["role"] in {"user", "assistant"}
    ]
    assert provider.seen_user_counts == [1, 2]
    assert persisted == [
        ("user", "first"),
        ("assistant", "reply-1"),
        ("user", "second"),
        ("assistant", "reply-2"),
    ]


@pytest.mark.asyncio
async def test_run_agent_loop_retries_blank_final_response(tmp_path) -> None:
    provider = _SequenceProvider([
        LLMResponse(content="   ", finish_reason="stop"),
        LLMResponse(content="recovered", finish_reason="stop"),
    ])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, max_iterations=4)

    final_content, _, messages, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "answer"}],
    )

    assert final_content == "recovered"
    assert len(provider.calls) == 2
    assert any(
        message.get("role") == "user" and "empty response" in str(message.get("content", "")).lower()
        for message in messages
    )


@pytest.mark.asyncio
async def test_run_agent_loop_recovers_length_truncated_response(tmp_path) -> None:
    provider = _SequenceProvider([
        LLMResponse(content="part one", finish_reason="length"),
        LLMResponse(content=" and part two", finish_reason="stop"),
    ])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, max_iterations=4)

    final_content, _, messages, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "write long"}],
    )

    assert final_content == "part one and part two"
    assert len(provider.calls) == 2
    assert any(
        message.get("role") == "user" and "continue exactly where you left off" in str(message.get("content", "")).lower()
        for message in messages
    )
