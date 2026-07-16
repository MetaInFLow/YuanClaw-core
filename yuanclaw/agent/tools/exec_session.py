"""Session support for long-running exec workflows."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from yuanclaw.agent.tools.base import Tool

DEFAULT_YIELD_MS = 1000
MAX_YIELD_MS = 30_000
DEFAULT_MAX_OUTPUT_CHARS = 10_000
MAX_OUTPUT_CHARS = 50_000


def subprocess_group_kwargs() -> dict[str, Any]:
    """Start a shell in a group that can be terminated with all descendants."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and all descendants, then reap the root process."""
    if process.returncode is not None:
        await process.wait()
        return
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=5.0)


@dataclass(slots=True)
class _SessionPoll:
    output: str
    done: bool
    exit_code: int | None
    elapsed_s: float = 0.0
    timed_out: bool = False
    terminated: bool = False
    stdin_closed: bool = False
    truncated_chars: int = 0


@dataclass(slots=True)
class ExecSessionInfo:
    session_id: str
    command: str
    cwd: str
    elapsed_s: float
    idle_s: float
    remaining_s: float
    returncode: int | None


class _ExecSession:
    def __init__(
        self,
        *,
        session_id: str,
        owner_key: str,
        process: asyncio.subprocess.Process,
        command: str,
        cwd: str,
        timeout: int | None,
    ) -> None:
        self.session_id = session_id
        self.owner_key = owner_key
        self.process = process
        self.command = command
        self.cwd = cwd
        self.started_at = time.monotonic()
        self.deadline = time.monotonic() + timeout if timeout else float("inf")
        self.last_access = time.monotonic()
        self._chunks: list[str] = []
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._io_finished = False
        self._timed_out = False
        self._stdout_task = asyncio.create_task(self._read_stream(process.stdout, ""))
        self._stderr_task = asyncio.create_task(self._read_stream(process.stderr, "STDERR:\n"))
        self._deadline_task = (
            asyncio.create_task(self._watch_deadline(timeout))
            if timeout is not None and timeout > 0
            else None
        )

    async def _watch_deadline(self, timeout: int) -> None:
        await asyncio.sleep(timeout)
        if self.process.returncode is None:
            self._timed_out = True
            await self.kill()

    async def _read_stream(self, stream: asyncio.StreamReader | None, prefix: str) -> None:
        if stream is None:
            return
        first = True
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if prefix and first:
                text = prefix + text
                first = False
            async with self._lock:
                self._chunks.append(text)

    async def write(self, chars: str) -> str | None:
        if self.process.returncode is not None:
            return "session has already exited"
        if self.process.stdin is None:
            return "session stdin is not available"
        try:
            self.process.stdin.write(chars.encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return "session stdin is closed"
        return None

    async def close_stdin(self) -> str | None:
        if self.process.returncode is not None:
            return "session has already exited"
        if self.process.stdin is None:
            return "session stdin is not available"
        self.process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await self.process.stdin.wait_closed()
        return None

    async def _finish_io(self) -> None:
        """Reap the process and close reader tasks before releasing the session."""
        async with self._lifecycle_lock:
            await self._finish_io_locked()

    async def _finish_io_locked(self) -> None:
        if self._io_finished:
            return
        current = asyncio.current_task()
        if (
            self._deadline_task is not None
            and self._deadline_task is not current
            and not self._deadline_task.done()
        ):
            self._deadline_task.cancel()
            await asyncio.gather(self._deadline_task, return_exceptions=True)
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await self.process.stdin.wait_closed()

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=2.0)

        readers = (self._stdout_task, self._stderr_task)
        done, pending = await asyncio.wait(readers, timeout=2.0)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        self._io_finished = True

    async def poll(
        self,
        yield_time_ms: int,
        max_output_chars: int,
        *,
        terminated: bool = False,
        stdin_closed: bool = False,
    ) -> _SessionPoll:
        self.last_access = time.monotonic()
        if yield_time_ms > 0 and self.process.returncode is None:
            await asyncio.sleep(min(yield_time_ms, MAX_YIELD_MS) / 1000)

        if self.process.returncode is None and time.monotonic() >= self.deadline:
            self._timed_out = True
            await self.kill()

        if self.process.returncode is not None:
            await self._finish_io()

        async with self._lock:
            output = "".join(self._chunks)
            self._chunks.clear()

        output, truncated = _truncate_output(output, max_output_chars)
        return _SessionPoll(
            output=output,
            done=self.process.returncode is not None,
            exit_code=self.process.returncode,
            elapsed_s=max(0.0, time.monotonic() - self.started_at),
            timed_out=self._timed_out,
            terminated=terminated,
            stdin_closed=stdin_closed,
            truncated_chars=truncated,
        )

    async def kill(self) -> None:
        async with self._lifecycle_lock:
            if self.process.returncode is None:
                await terminate_process_tree(self.process)
            await self._finish_io_locked()


class ExecSessionManager:
    def __init__(self, *, max_sessions: int = 8, idle_timeout: int = 1800) -> None:
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self._sessions: dict[str, _ExecSession] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        owner_key: str,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: int | None,
        yield_time_ms: int,
        max_output_chars: int,
    ) -> tuple[str, _SessionPoll]:
        async with self._lock:
            await self._cleanup_locked()
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(f"maximum exec sessions reached ({self.max_sessions})")
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                **subprocess_group_kwargs(),
            )
            session_id = uuid.uuid4().hex[:12]
            session = _ExecSession(
                session_id=session_id,
                owner_key=owner_key,
                process=process,
                command=command,
                cwd=cwd,
                timeout=timeout,
            )
            self._sessions[session_id] = session

        poll = await session.poll(yield_time_ms, max_output_chars)
        if poll.done:
            async with self._lock:
                self._sessions.pop(session_id, None)
        return session_id, poll

    async def write(
        self,
        *,
        owner_key: str,
        session_id: str,
        chars: str | None,
        close_stdin: bool,
        terminate: bool,
        yield_time_ms: int,
        max_output_chars: int,
    ) -> _SessionPoll:
        async with self._lock:
            await self._cleanup_locked()
            session = self._sessions.get(session_id)
        if session is None or session.owner_key != owner_key:
            raise KeyError(session_id)

        if chars:
            error = await session.write(chars)
            if error:
                raise RuntimeError(error)
        stdin_closed = False
        if close_stdin:
            error = await session.close_stdin()
            if error:
                raise RuntimeError(error)
            stdin_closed = True
        if terminate:
            await session.kill()
        poll = await session.poll(
            yield_time_ms,
            max_output_chars,
            terminated=terminate,
            stdin_closed=stdin_closed,
        )
        if poll.done:
            async with self._lock:
                self._sessions.pop(session_id, None)
        return poll

    async def list(self, *, owner_key: str) -> list[ExecSessionInfo]:
        async with self._lock:
            await self._cleanup_locked()
            now = time.monotonic()
            return [
                ExecSessionInfo(
                    session_id=session_id,
                    command=session.command,
                    cwd=session.cwd,
                    elapsed_s=max(0.0, now - session.started_at),
                    idle_s=max(0.0, now - session.last_access),
                    remaining_s=max(0.0, session.deadline - now),
                    returncode=session.process.returncode,
                )
                for session_id, session in sorted(self._sessions.items())
                if session.owner_key == owner_key
            ]

    async def terminate_owner(self, owner_key: str) -> int:
        """Terminate and remove every session owned by one chat session."""
        async with self._lock:
            sessions = [
                self._sessions.pop(session_id)
                for session_id, session in list(self._sessions.items())
                if session.owner_key == owner_key
            ]
        if sessions:
            await asyncio.gather(*(session.kill() for session in sessions), return_exceptions=True)
        return len(sessions)

    async def terminate_all(self) -> int:
        """Terminate and remove all managed exec sessions."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        if sessions:
            await asyncio.gather(*(session.kill() for session in sessions), return_exceptions=True)
        return len(sessions)

    async def _cleanup_locked(self) -> None:
        now = time.monotonic()
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_access > self.idle_timeout
        ]
        for session_id in stale:
            session = self._sessions.pop(session_id)
            await session.kill()


DEFAULT_EXEC_SESSION_MANAGER = ExecSessionManager()


def clamp_session_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    return min(max(value, minimum), maximum)


def _truncate_output(output: str, max_output_chars: int) -> tuple[str, int]:
    if len(output) <= max_output_chars:
        return output, 0
    half = max_output_chars // 2
    omitted = len(output) - max_output_chars
    return (
        output[:half]
        + f"\n\n... ({omitted:,} chars truncated) ...\n\n"
        + output[-half:],
        omitted,
    )


def format_session_poll(session_id: str, poll: _SessionPoll) -> str:
    parts = [poll.output] if poll.output else []
    if poll.truncated_chars:
        parts.append(f"(output truncated by {poll.truncated_chars:,} chars)")
    if poll.timed_out:
        parts.append("Error: Command timed out; session was terminated.")
    if poll.terminated and not poll.timed_out:
        parts.append("Session terminated.")
    if poll.stdin_closed:
        parts.append("Stdin closed.")
    if poll.done:
        parts.append(f"Exit code: {poll.exit_code}")
    else:
        parts.append(f"Process running. session_id: {session_id}")
    parts.append(f"Elapsed: {poll.elapsed_s:.1f}s")
    return "\n".join(parts) if parts else "(no output yet)"


class WriteStdinTool(Tool):
    """Write to or poll a running exec session."""

    def __init__(self, *, manager: ExecSessionManager | None = None) -> None:
        self._manager = manager or DEFAULT_EXEC_SESSION_MANAGER
        self._owner_key: ContextVar[str] = ContextVar(
            f"exec_session_owner_{id(self)}",
            default="cli:direct",
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        self._owner_key.set(f"{channel}:{chat_id}")

    @property
    def name(self) -> str:
        return "write_stdin"

    @property
    def description(self) -> str:
        return (
            "Interact with a running exec session created by exec with yield_time_ms. "
            "Use chars='' to poll, chars to send stdin, close_stdin=true to send EOF, "
            "or terminate=true to stop the process."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "chars": {"type": ["string", "null"]},
                "close_stdin": {"type": "boolean"},
                "terminate": {"type": "boolean"},
                "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": MAX_YIELD_MS},
                "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": MAX_OUTPUT_CHARS},
                "max_output_tokens": {"type": ["integer", "null"], "minimum": 1000, "maximum": MAX_OUTPUT_CHARS},
            },
            "required": ["session_id"],
        }

    async def execute(
        self,
        session_id: str,
        chars: str | None = None,
        close_stdin: bool = False,
        terminate: bool = False,
        yield_time_ms: int | None = None,
        max_output_chars: int | None = None,
        max_output_tokens: int | None = None,
        **_kwargs: Any,
    ) -> str:
        try:
            if max_output_chars is None:
                max_output_chars = max_output_tokens
            poll = await self._manager.write(
                owner_key=self._owner_key.get(),
                session_id=session_id,
                chars=chars,
                close_stdin=close_stdin,
                terminate=terminate,
                yield_time_ms=clamp_session_int(yield_time_ms, DEFAULT_YIELD_MS, 0, MAX_YIELD_MS),
                max_output_chars=clamp_session_int(
                    max_output_chars, DEFAULT_MAX_OUTPUT_CHARS, 1000, MAX_OUTPUT_CHARS
                ),
            )
            return format_session_poll(session_id, poll)
        except KeyError:
            return f"Error: exec session not found: {session_id}"
        except Exception as exc:
            return f"Error writing to exec session: {exc}"


class ListExecSessionsTool(WriteStdinTool):
    """List active exec sessions."""

    @property
    def name(self) -> str:
        return "list_exec_sessions"

    @property
    def description(self) -> str:
        return "List active long-running exec sessions."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: Any) -> str:
        sessions = await self._manager.list(owner_key=self._owner_key.get())
        if not sessions:
            return "No active exec sessions."
        lines = []
        for info in sessions:
            command = " ".join(info.command.split())
            if len(command) > 120:
                command = command[:119] + "..."
            status = "exited" if info.returncode is not None else "running"
            lines.append(
                f"{info.session_id} | {status} | elapsed={info.elapsed_s:.1f}s "
                f"| idle={info.idle_s:.1f}s | remaining={info.remaining_s:.1f}s "
                f"| cwd={info.cwd} | {command}"
            )
        return "\n".join(lines)
