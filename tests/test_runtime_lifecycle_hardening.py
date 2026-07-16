from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.exec_session import ExecSessionManager
from yuanclaw.agent.tools.long_task import LongTaskTool
from yuanclaw.agent.tools.message import MessageTool
from yuanclaw.agent.tools.shell import ExecTool
from yuanclaw.bus.events import OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMProvider, LLMResponse
from yuanclaw.session.manager import SessionManager


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def get_default_model(self) -> str:
        return "test"

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_message_tool_context_is_task_local() -> None:
    sent: list[tuple[str, str, str]] = []
    release = asyncio.Event()
    ready = 0
    both_ready = asyncio.Event()

    async def send(message: OutboundMessage) -> None:
        sent.append((message.channel, message.chat_id, message.content))

    tool = MessageTool(send_callback=send)

    async def run(channel: str, chat_id: str) -> None:
        nonlocal ready
        tool.set_context(channel, chat_id)
        tool.start_turn()
        ready += 1
        if ready == 2:
            both_ready.set()
        await release.wait()
        await tool.execute(content=chat_id)
        assert tool._sent_in_turn is True

    first = asyncio.create_task(run("studio", "first"))
    second = asyncio.create_task(run("telegram", "second"))
    await asyncio.wait_for(both_ready.wait(), timeout=1.0)
    release.set()
    await asyncio.gather(first, second)

    assert sorted(sent) == [
        ("studio", "first", "first"),
        ("telegram", "second", "second"),
    ]


@pytest.mark.asyncio
async def test_long_task_context_is_task_local(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    tool = LongTaskTool(sessions)
    release = asyncio.Event()
    ready = 0
    both_ready = asyncio.Event()

    async def run(chat_id: str) -> None:
        nonlocal ready
        tool.set_context("studio", chat_id)
        ready += 1
        if ready == 2:
            both_ready.set()
        await release.wait()
        result = await tool.execute(goal=f"goal-{chat_id}")
        assert result.startswith("Goal recorded")

    first = asyncio.create_task(run("first"))
    second = asyncio.create_task(run("second"))
    await asyncio.wait_for(both_ready.wait(), timeout=1.0)
    release.set()
    await asyncio.gather(first, second)

    assert sessions.get_or_create("studio:first").metadata["goal_state"]["objective"] == "goal-first"
    assert sessions.get_or_create("studio:second").metadata["goal_state"]["objective"] == "goal-second"


@pytest.mark.asyncio
async def test_process_direct_is_registered_and_cancelled_by_session(tmp_path) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_Provider(), workspace=tmp_path)
    started = asyncio.Event()

    async def slow_process(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    loop._process_message = slow_process  # type: ignore[method-assign]
    task = asyncio.create_task(loop.process_direct("hello", session_key="studio:thread"))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert task in loop._active_tasks["studio:thread"]
    assert await loop.cancel_session("studio:thread") == 1
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "studio:thread" not in loop._active_tasks
    assert "studio:thread" not in loop._session_locks


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_background_tasks(tmp_path) -> None:
    provider = _Provider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def background() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loop._schedule_background(background())
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await loop.shutdown()

    assert cancelled.is_set()
    assert loop._background_tasks == set()
    assert provider.closed is True


@pytest.mark.asyncio
async def test_exec_session_deadline_expires_without_polling(tmp_path) -> None:
    manager = ExecSessionManager(idle_timeout=60)
    code = "import sys; print('ready', flush=True); sys.stdin.readline()"
    session_id, poll = await manager.start(
        owner_key="studio:thread",
        command=subprocess.list2cmdline([sys.executable, "-c", code]),
        cwd=str(tmp_path),
        env={},
        timeout=1,
        yield_time_ms=50,
        max_output_chars=2000,
    )
    assert poll.done is False

    await asyncio.sleep(1.5)
    sessions = await manager.list(owner_key="studio:thread")

    assert sessions[0].session_id == session_id
    assert sessions[0].returncode is not None
    await manager.terminate_all()


@pytest.mark.asyncio
async def test_exec_session_concurrent_termination_is_idempotent(tmp_path) -> None:
    manager = ExecSessionManager(idle_timeout=60)
    code = "import sys; sys.stdin.readline()"
    await manager.start(
        owner_key="studio:thread",
        command=subprocess.list2cmdline([sys.executable, "-c", code]),
        cwd=str(tmp_path),
        env={},
        timeout=30,
        yield_time_ms=20,
        max_output_chars=2000,
    )

    counts = await asyncio.gather(
        manager.terminate_owner("studio:thread"),
        manager.terminate_all(),
    )

    assert sum(counts) == 1


@pytest.mark.asyncio
async def test_shell_cancellation_terminates_descendant_process(tmp_path) -> None:
    marker = tmp_path / "descendant-finished.txt"
    parent_script = tmp_path / "parent.py"
    child_code = (
        "import pathlib,time; time.sleep(1.5); "
        f"pathlib.Path({str(marker)!r}).write_text('finished', encoding='utf-8')"
    )
    parent_script.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    tool = ExecTool(working_dir=str(tmp_path), timeout=60)
    command = subprocess.list2cmdline([sys.executable, str(parent_script)])

    task = asyncio.create_task(tool.execute(command=command))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.7)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_streaming_llm_call_keeps_wall_timeout(tmp_path) -> None:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_Provider(),
        workspace=tmp_path,
        llm_timeout_s=0.01,
    )

    async def never_returns() -> LLMResponse:
        await asyncio.Event().wait()
        return LLMResponse(content="unreachable")

    response = await loop._await_llm_response(
        never_returns(),
        llm_timeout_s=None,
        streaming=True,
    )

    assert response.error_kind == "timeout"
    assert "timed out after 0.01s" in (response.content or "")
