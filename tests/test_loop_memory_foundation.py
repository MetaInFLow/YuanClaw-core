from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.bus.events import InboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.config.schema import MemoryConfig
from yuanclaw.providers.base import LLMResponse


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_loop(tmp_path: Path, *, memory_config: MemoryConfig) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        memory_window=10,
        memory_config=memory_config,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


def test_loop_registers_memory_tools(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, memory_config=MemoryConfig(backend="core"))

    assert loop.tools.has("memory_search")
    assert loop.tools.has("memory_get")


@pytest.mark.asyncio
async def test_loop_main_private_session_injects_recent_memory(tmp_path: Path) -> None:
    _write(tmp_path / "MEMORY.md", "# MEMORY\n\nalpha durable fact\n")
    _write(tmp_path / "memory" / "2026-03-23.md", "# 2026-03-23\n\nbeta daily note\n")
    loop = _make_loop(tmp_path, memory_config=MemoryConfig(backend="core", recent_days=2))

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="chat-1", content="hello")
    await loop._process_message(msg)

    system_prompt = loop.provider.chat.await_args.kwargs["messages"][0]["content"]
    assert "alpha durable fact" in system_prompt
    assert "beta daily note" in system_prompt


@pytest.mark.asyncio
async def test_loop_non_main_private_session_skips_recent_memory(tmp_path: Path) -> None:
    _write(tmp_path / "MEMORY.md", "# MEMORY\n\nalpha durable fact\n")
    _write(tmp_path / "memory" / "2026-03-23.md", "# 2026-03-23\n\nbeta daily note\n")
    loop = _make_loop(tmp_path, memory_config=MemoryConfig(backend="core", recent_days=2))

    msg = InboundMessage(
        channel="telegram",
        sender_id="user",
        chat_id="chat-1",
        content="hello",
        session_key_override="telegram:chat-1:thread-1",
    )
    await loop._process_message(msg)

    system_prompt = loop.provider.chat.await_args.kwargs["messages"][0]["content"]
    assert "alpha durable fact" in system_prompt
    assert "beta daily note" not in system_prompt


@pytest.mark.asyncio
async def test_loop_group_session_hides_private_memory(tmp_path: Path) -> None:
    _write(tmp_path / "MEMORY.md", "# MEMORY\n\nalpha durable fact\n")
    _write(tmp_path / "memory" / "2026-03-23.md", "# 2026-03-23\n\nbeta daily note\n")
    loop = _make_loop(tmp_path, memory_config=MemoryConfig(backend="core", recent_days=2))

    msg = InboundMessage(
        channel="telegram",
        sender_id="user",
        chat_id="group-1",
        content="hello",
        metadata={"is_group": True},
    )
    await loop._process_message(msg)

    system_prompt = loop.provider.chat.await_args.kwargs["messages"][0]["content"]
    assert "alpha durable fact" not in system_prompt
    assert "beta daily note" not in system_prompt
