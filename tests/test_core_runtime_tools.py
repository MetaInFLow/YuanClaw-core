from pathlib import Path

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.apply_patch import ApplyPatchTool
from yuanclaw.agent.tools.exec_session import (
    ExecSessionManager,
    ListExecSessionsTool,
    WriteStdinTool,
)
from yuanclaw.agent.tools.shell import ExecTool
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    async def chat(self, **_kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


@pytest.mark.asyncio
async def test_apply_patch_replaces_and_adds_files(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(
        edits=[
            {
                "path": "hello.txt",
                "action": "replace",
                "old_text": "world\n",
                "new_text": "yuanclaw\n",
            },
            {
                "path": "notes/new.txt",
                "action": "add",
                "new_text": "created\n",
            },
        ]
    )

    assert "Patch applied:" in result
    assert "- update hello.txt" in result
    assert "- add notes/new.txt" in result
    assert target.read_text(encoding="utf-8") == "hello\nyuanclaw\n"
    assert (tmp_path / "notes" / "new.txt").read_text(encoding="utf-8") == "created\n"


@pytest.mark.asyncio
async def test_apply_patch_dry_run_does_not_write(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hello\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(
        edits=[
            {
                "path": "hello.txt",
                "action": "replace",
                "old_text": "hello\n",
                "new_text": "changed\n",
            }
        ],
        dry_run=True,
    )

    assert "Patch dry-run succeeded:" in result
    assert target.read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_apply_patch_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(
        edits=[
            {
                "path": "../outside.txt",
                "action": "replace",
                "old_text": "outside\n",
                "new_text": "changed\n",
            }
        ]
    )

    assert result.startswith("Error:")
    assert "must not contain '..'" in result
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.asyncio
async def test_apply_patch_does_not_partially_write_when_later_target_fails(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    first.write_text("old\n", encoding="utf-8")
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory\n", encoding="utf-8")
    tool = ApplyPatchTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(
        edits=[
            {
                "path": "first.txt",
                "action": "replace",
                "old_text": "old\n",
                "new_text": "new\n",
            },
            {
                "path": "blocked/child.txt",
                "action": "add",
                "new_text": "created\n",
            },
        ]
    )

    assert result.startswith("Error:")
    assert first.read_text(encoding="utf-8") == "old\n"
    assert blocked_parent.read_text(encoding="utf-8") == "not a directory\n"


@pytest.mark.asyncio
async def test_apply_patch_preserves_crlf_when_appending_to_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    tool = ApplyPatchTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(
        edits=[
            {
                "path": "windows.txt",
                "action": "add",
                "new_text": "three\r\n",
            }
        ]
    )

    assert "Patch applied:" in result
    assert target.read_bytes() == b"one\r\ntwo\r\nthree\r\n"


@pytest.mark.asyncio
async def test_exec_session_supports_start_poll_stdin_and_list(tmp_path: Path) -> None:
    manager = ExecSessionManager(max_sessions=4, idle_timeout=60)
    exec_tool = ExecTool(
        working_dir=str(tmp_path),
        timeout=10,
        restrict_to_workspace=True,
        session_manager=manager,
    )
    write_tool = WriteStdinTool(manager=manager)
    list_tool = ListExecSessionsTool(manager=manager)
    code = (
        "import sys, time; "
        "print('ready', flush=True); "
        "data=sys.stdin.readline().strip(); "
        "print('got:'+data, flush=True); "
        "time.sleep(0.05)"
    )

    started = await exec_tool.execute(
        command=f"python -c {code!r}",
        yield_time_ms=50,
        max_output_chars=2000,
    )
    assert "Process running. session_id:" in started
    session_id = started.split("session_id:", 1)[1].splitlines()[0].strip()

    listed = await list_tool.execute()
    assert session_id in listed

    finished = await write_tool.execute(
        session_id=session_id,
        chars="hello\n",
        close_stdin=True,
        yield_time_ms=100,
        max_output_chars=2000,
    )
    output_parts = [finished]

    for _ in range(20):
        if "Process running. session_id:" not in finished:
            break
        finished = await write_tool.execute(
            session_id=session_id,
            yield_time_ms=100,
            max_output_chars=2000,
        )
        output_parts.append(finished)

    if "Process running. session_id:" in finished:
        await write_tool.execute(
            session_id=session_id,
            terminate=True,
            yield_time_ms=0,
            max_output_chars=2000,
        )

    combined_output = "\n".join(output_parts)
    assert "got:hello" in combined_output
    assert "Exit code: 0" in combined_output


@pytest.mark.asyncio
async def test_exec_tool_rejects_working_dir_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = ExecTool(
        working_dir=str(workspace),
        timeout=10,
        restrict_to_workspace=True,
    )

    result = await tool.execute(command="pwd", working_dir=str(outside))

    assert result.startswith("Error:")
    assert "working_dir is outside the configured workspace" in result


@pytest.mark.asyncio
async def test_exec_tool_rejects_yielding_session_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = ExecTool(
        working_dir=str(workspace),
        timeout=10,
        restrict_to_workspace=True,
        session_manager=ExecSessionManager(),
    )

    result = await tool.execute(command="pwd", working_dir=str(outside), yield_time_ms=1)

    assert result.startswith("Error:")
    assert "working_dir is outside the configured workspace" in result


@pytest.mark.asyncio
async def test_exec_tool_does_not_forward_arbitrary_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YUANCLAW_SHOULD_NOT_LEAK", "secret")
    tool = ExecTool(working_dir=str(tmp_path), timeout=10)
    code = "import os; print(os.environ.get('YUANCLAW_SHOULD_NOT_LEAK', 'missing'))"

    result = await tool.execute(command=f"python -c {code!r}")

    assert "missing" in result
    assert "secret" not in result


@pytest.mark.asyncio
async def test_agent_loop_exec_sessions_are_isolated_between_loops(tmp_path: Path) -> None:
    loop_a = AgentLoop(bus=MessageBus(), provider=_FakeProvider(), workspace=tmp_path / "a")
    loop_b = AgentLoop(bus=MessageBus(), provider=_FakeProvider(), workspace=tmp_path / "b")
    exec_tool = loop_a.tools.get("exec")
    list_tool = loop_b.tools.get("list_exec_sessions")
    write_tool = loop_b.tools.get("write_stdin")
    assert exec_tool is not None
    assert list_tool is not None
    assert write_tool is not None
    code = "import sys; print('ready', flush=True); sys.stdin.readline()"

    started = await exec_tool.execute(command=f"python -c {code!r}", yield_time_ms=50)
    assert "Process running. session_id:" in started
    session_id = started.split("session_id:", 1)[1].splitlines()[0].strip()

    assert await list_tool.execute() == "No active exec sessions."
    assert await write_tool.execute(session_id=session_id, terminate=True, yield_time_ms=0) == (
        f"Error: exec session not found: {session_id}"
    )
    await loop_a.tools.get("write_stdin").execute(
        session_id=session_id,
        terminate=True,
        yield_time_ms=0,
    )


@pytest.mark.asyncio
async def test_agent_loop_exec_sessions_are_isolated_between_sessions(tmp_path: Path) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_FakeProvider(), workspace=tmp_path)
    exec_tool = loop.tools.get("exec")
    list_tool = loop.tools.get("list_exec_sessions")
    write_tool = loop.tools.get("write_stdin")
    assert exec_tool is not None
    assert list_tool is not None
    assert write_tool is not None
    code = "import sys; print('ready', flush=True); sys.stdin.readline()"

    loop._set_tool_context("studio", "thread-a")
    started = await exec_tool.execute(command=f"python -c {code!r}", yield_time_ms=50)
    assert "Process running. session_id:" in started
    session_id = started.split("session_id:", 1)[1].splitlines()[0].strip()

    loop._set_tool_context("studio", "thread-b")
    assert await list_tool.execute() == "No active exec sessions."
    assert await write_tool.execute(session_id=session_id, terminate=True, yield_time_ms=0) == (
        f"Error: exec session not found: {session_id}"
    )

    loop._set_tool_context("studio", "thread-a")
    terminated = await write_tool.execute(session_id=session_id, terminate=True, yield_time_ms=0)
    assert "Session terminated." in terminated
    assert "Exit code:" in terminated
    assert await list_tool.execute() == "No active exec sessions."


def test_agent_loop_registers_core_runtime_tools(tmp_path: Path) -> None:
    loop = AgentLoop(bus=MessageBus(), provider=_FakeProvider(), workspace=tmp_path)

    assert loop.tools.get("apply_patch") is not None
    assert loop.tools.get("write_stdin") is not None
    assert loop.tools.get("list_exec_sessions") is not None
