"""Test message tool suppress logic for final replies."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.message import MessageTool
from yuanclaw.agent.tools.registry import ToolExecutionResult
from yuanclaw.bus.events import InboundMessage, OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model", memory_window=10)


class TestMessageToolSuppressLogic:
    """Final reply suppressed only when message tool sends to the same target."""

    @pytest.mark.asyncio
    async def test_suppress_when_sent_to_same_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Hello", "channel": "feishu", "chat_id": "chat123"},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        sent: list[OutboundMessage] = []
        mt = loop.tools.get("message")
        if isinstance(mt, MessageTool):
            mt.set_send_callback(AsyncMock(side_effect=lambda m: sent.append(m)))

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert result is None  # suppressed

    @pytest.mark.asyncio
    async def test_not_suppress_when_sent_to_different_target(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1", name="message",
            arguments={"content": "Email content", "channel": "email", "chat_id": "user@example.com"},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="I've sent the email.", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])

        sent: list[OutboundMessage] = []
        mt = loop.tools.get("message")
        if isinstance(mt, MessageTool):
            mt.set_send_callback(AsyncMock(side_effect=lambda m: sent.append(m)))

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Send email")
        result = await loop._process_message(msg)

        assert len(sent) == 1
        assert sent[0].channel == "email"
        assert result is not None  # not suppressed
        assert result.channel == "feishu"

    @pytest.mark.asyncio
    async def test_not_suppress_when_no_message_tool_used(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop.provider.chat = AsyncMock(return_value=LLMResponse(content="Hello!", tool_calls=[]))
        loop.tools.get_definitions = MagicMock(return_value=[])

        msg = InboundMessage(channel="feishu", sender_id="user1", chat_id="chat123", content="Hi")
        result = await loop._process_message(msg)

        assert result is not None
        assert "Hello" in result.content

    async def test_progress_hides_internal_reasoning(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="read_file", arguments={"path": "foo.txt"})
        calls = iter([
            LLMResponse(
                content="Visible<think>hidden</think>",
                tool_calls=[tool_call],
                reasoning_content="secret reasoning",
                thinking_blocks=[{"signature": "sig", "thought": "secret thought"}],
            ),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute_result = AsyncMock(
            return_value=ToolExecutionResult(content="ok")
        )

        progress: list[tuple[str, bool]] = []

        async def on_progress(content: str, *, tool_hint: bool = False) -> None:
            progress.append((content, tool_hint))

        final_content, _, _, _ = await loop._run_agent_loop([], on_progress=on_progress)

        assert final_content == "Done"
        assert progress == [
            ("Visible", False),
            ('read_file("foo.txt")', True),
        ]

    async def test_progress_includes_structured_tool_start_and_finish_events(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="read_file", arguments={"path": "foo.txt"})
        calls = iter([
            LLMResponse(content="Checking", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute_result = AsyncMock(
            return_value=ToolExecutionResult(content="file contents")
        )

        progress: list[dict] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            progress.append(
                {
                    "content": content,
                    "tool_hint": tool_hint,
                    "tool_events": tool_events,
                }
            )

        final_content, _, _, _ = await loop._run_agent_loop([], on_progress=on_progress)

        assert final_content == "Done"
        assert progress[1]["tool_hint"] is True
        assert progress[1]["tool_events"] == [
            {
                "version": 1,
                "phase": "start",
                "call_id": "call1",
                "name": "read_file",
                "arguments": {"path": "foo.txt"},
                "result": None,
                "error": None,
                "files": [],
                "embeds": [],
            }
        ]
        assert progress[2]["tool_events"] == [
            {
                "version": 1,
                "phase": "end",
                "call_id": "call1",
                "name": "read_file",
                "arguments": {"path": "foo.txt"},
                "result": "file contents",
                "error": None,
                "files": [],
                "embeds": [],
            }
        ]

    async def test_progress_includes_apply_patch_file_edit_events(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1",
            name="apply_patch",
            arguments={
                "edits": [
                    {
                        "path": "demo.txt",
                        "action": "replace",
                        "old_text": "old",
                        "new_text": "new",
                    }
                ]
            },
        )
        calls = iter([
            LLMResponse(content="Editing", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute_result = AsyncMock(
            return_value=ToolExecutionResult(
                content="Patch applied:\n- update demo.txt (+1/-1)"
            )
        )

        progress: list[dict] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
            file_edit_events: list[dict] | None = None,
        ) -> None:
            progress.append(
                {
                    "content": content,
                    "tool_hint": tool_hint,
                    "tool_events": tool_events,
                    "file_edit_events": file_edit_events,
                }
            )

        final_content, _, _, _ = await loop._run_agent_loop([], on_progress=on_progress)

        assert final_content == "Done"
        assert progress[1]["file_edit_events"] == [
            {
                "version": 1,
                "phase": "start",
                "call_id": "call1",
                "tool": "apply_patch",
                "path": "demo.txt",
                "action": "replace",
                "error": None,
            }
        ]
        assert progress[2]["file_edit_events"] == [
            {
                "version": 1,
                "phase": "end",
                "call_id": "call1",
                "tool": "apply_patch",
                "path": "demo.txt",
                "action": "replace",
                "error": None,
            }
        ]

    async def test_progress_marks_apply_patch_file_edit_errors(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(
            id="call1",
            name="apply_patch",
            arguments={"edits": [{"path": "demo.txt", "action": "replace"}]},
        )
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.execute_result = AsyncMock(
            return_value=ToolExecutionResult(
                content="Error: old_text required",
                error="Error: old_text required",
            )
        )

        progress: list[list[dict] | None] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            file_edit_events: list[dict] | None = None,
        ) -> None:
            progress.append(file_edit_events)

        await loop._run_agent_loop([], on_progress=on_progress)

        assert progress[-1] == [
            {
                "version": 1,
                "phase": "error",
                "call_id": "call1",
                "tool": "apply_patch",
                "path": "demo.txt",
                "action": "replace",
                "error": "Error: old_text required",
            }
        ]


class TestMessageToolTurnTracking:

    def test_sent_in_turn_tracks_same_target(self) -> None:
        tool = MessageTool()
        tool.set_context("feishu", "chat1")
        assert not tool._sent_in_turn
        tool._sent_in_turn = True
        assert tool._sent_in_turn

    def test_start_turn_resets(self) -> None:
        tool = MessageTool()
        tool._sent_in_turn = True
        tool.start_turn()
        assert not tool._sent_in_turn
