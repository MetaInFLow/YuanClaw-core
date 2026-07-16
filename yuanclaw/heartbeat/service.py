"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from yuanclaw.providers.base import LLMProvider

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        decision_timeout_s: float = 60.0,
        execution_timeout_s: float = 600.0,
        notify_timeout_s: float = 30.0,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.decision_timeout_s = decision_timeout_s
        self.execution_timeout_s = execution_timeout_s
        self.notify_timeout_s = notify_timeout_s
        self._running = False
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": "You are a heartbeat agent. Call the heartbeat tool to report your decision."},
                {"role": "user", "content": (
                    "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
                    f"{content}"
                )},
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )

        if not response.has_tool_calls:
            return "skip", ""

        tool_call = response.tool_calls[0]
        if tool_call.name != "heartbeat" or not isinstance(tool_call.arguments, dict):
            return "skip", ""
        action = tool_call.arguments.get("action")
        tasks = tool_call.arguments.get("tasks", "")
        if action not in {"skip", "run"}:
            return "skip", ""
        if action == "run" and (not isinstance(tasks, str) or not tasks.strip()):
            return "skip", ""
        return action, tasks.strip() if isinstance(tasks, str) else ""

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def aclose(self) -> None:
        """Stop the service and wait for its loop task to finish."""
        task = self._task
        self.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        try:
            await self._run_once(notify=True)
        except TimeoutError as exc:
            logger.error("Heartbeat timed out: {}", exc)
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def _run_once(self, *, notify: bool) -> str | None:
        if self._run_lock.locked():
            logger.info("Heartbeat: skipped overlapping trigger")
            return None
        async with self._run_lock:
            return await self._run_once_locked(notify=notify)

    async def _run_once_locked(self, *, notify: bool) -> str | None:
        content = self._read_heartbeat_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return None

        logger.info("Heartbeat: checking for tasks...")

        try:
            async with asyncio.timeout(self.decision_timeout_s):
                action, tasks = await self._decide(content)
        except TimeoutError:
            raise TimeoutError(f"decision exceeded {self.decision_timeout_s:g}s") from None

        if action != "run":
            logger.info("Heartbeat: OK (nothing to report)")
            return None

        logger.info("Heartbeat: tasks found, executing...")
        if self.on_execute:
            try:
                async with asyncio.timeout(self.execution_timeout_s):
                    response = await self.on_execute(tasks)
            except TimeoutError:
                raise TimeoutError(
                    f"execution exceeded {self.execution_timeout_s:g}s"
                ) from None
            if notify and response and self.on_notify:
                try:
                    async with asyncio.timeout(self.notify_timeout_s):
                        logger.info("Heartbeat: completed, delivering response")
                        await self.on_notify(response)
                except TimeoutError:
                    raise TimeoutError(
                        f"notification exceeded {self.notify_timeout_s:g}s"
                    ) from None
            return response
        return None

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        return await self._run_once(notify=False)
