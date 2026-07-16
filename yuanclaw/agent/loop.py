"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from yuanclaw.agent.autocompact import AutoCompact
from yuanclaw.agent.context import ContextBuilder
from yuanclaw.agent.memory import MemoryStore
from yuanclaw.agent.subagent import SubagentManager
from yuanclaw.agent.tools.apply_patch import ApplyPatchTool
from yuanclaw.agent.tools.cli_apps import CliAppsTool
from yuanclaw.agent.tools.cron import CronTool
from yuanclaw.agent.tools.exec_session import (
    ExecSessionManager,
    ListExecSessionsTool,
    WriteStdinTool,
)
from yuanclaw.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from yuanclaw.agent.tools.image_generation import ImageGenerationTool
from yuanclaw.agent.tools.long_task import CompleteGoalTool, LongTaskTool
from yuanclaw.agent.tools.memory import MemoryGetTool, MemorySearchTool
from yuanclaw.agent.tools.message import MessageTool
from yuanclaw.agent.tools.registry import ToolExecutionResult, ToolRegistry
from yuanclaw.agent.tools.shell import ExecTool
from yuanclaw.agent.tools.spawn import SpawnTool
from yuanclaw.agent.tools.web import WebFetchTool, WebSearchTool
from yuanclaw.apps.cli import normalize_cli_app_mentions
from yuanclaw.apps.cli import session_extra as cli_app_session_extra
from yuanclaw.apps.mcp_presets import (
    normalize_mcp_preset_mentions,
)
from yuanclaw.apps.mcp_presets import (
    session_extra as mcp_preset_session_extra,
)
from yuanclaw.bus.events import InboundMessage, OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.command import CommandContext, CommandRouter, register_builtin_commands
from yuanclaw.memory import CoreMemoryBackend, LegacyMemoryBackend, MemoryBackend
from yuanclaw.providers.base import LLMProvider, LLMResponse
from yuanclaw.security.workspace_access import (
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from yuanclaw.session.goal_state import runner_wall_llm_timeout_s, sustained_goal_active
from yuanclaw.session.manager import Session, SessionManager
from yuanclaw.studio_agents import get_fixed_skills_for_session
from yuanclaw.utils.helpers import strip_think

if TYPE_CHECKING:
    from yuanclaw.config.schema import (
        ChannelsConfig,
        CliAppsToolConfig,
        CompactionConfig,
        ExecToolConfig,
        ImageGenerationToolConfig,
        MemoryConfig,
        ProviderConfig,
    )
    from yuanclaw.cron.service import CronService


class _LoopIdleConsolidator:
    """Adapter that lets AutoCompact use the loop's existing memory consolidation stack."""

    def __init__(self, loop: "AgentLoop") -> None:
        self.loop = loop

    async def compact_idle_session(self, key: str, keep_recent_messages: int) -> str:
        lock = self.loop._consolidation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            session = self.loop.sessions.get_or_create(key)
            summary = await MemoryStore(self.loop.workspace).compact_idle_session(
                session,
                self.loop.provider,
                self.loop.model,
                keep_recent_messages=keep_recent_messages,
            )
            self.loop.sessions.save(session)
            return summary


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_BYTES = 500
    _TOOL_RESULT_TRUNCATION_SUFFIX = "\n... (truncated)"

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        provider_name: str | None = None,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
        llm_timeout_s: float | None = 300.0,
        memory_window: int = 100,
        memory_config: MemoryConfig | None = None,
        compaction_config: CompactionConfig | None = None,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_search_provider: str | None = None,
        web_search_base_url: str | None = None,
        web_search_max_results: int = 5,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        image_generation_config: ImageGenerationToolConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        cli_apps_config: CliAppsToolConfig | None = None,
        max_concurrent_subagents: int | None = None,
        subagent_timeout_s: float | None = None,
    ):
        from yuanclaw.config.schema import (
            CliAppsToolConfig,
            CompactionConfig,
            ExecToolConfig,
            ImageGenerationToolConfig,
            MemoryConfig,
        )
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.provider_name = provider_name
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.context_window_tokens = context_window_tokens or max_tokens
        self.llm_timeout_s = llm_timeout_s
        self.memory_window = memory_window
        self.memory_config = memory_config or MemoryConfig()
        self.compaction_config = compaction_config or CompactionConfig()
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_search_provider = web_search_provider
        self.web_search_base_url = web_search_base_url
        self.web_search_max_results = web_search_max_results
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.image_generation_config = image_generation_config or ImageGenerationToolConfig()
        self.image_generation_provider_configs = dict(image_generation_provider_configs or {})
        self.cli_apps_config = cli_apps_config or CliAppsToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}

        self.context = ContextBuilder(workspace)
        self.memory_backend = self._build_memory_backend(self.memory_config)
        self.sessions = session_manager or SessionManager(workspace)
        self.workspace_scope_resolver = WorkspaceScopeResolver(
            workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            brave_api_key=brave_api_key,
            web_search_provider=web_search_provider,
            web_search_base_url=web_search_base_url,
            web_search_max_results=web_search_max_results,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            llm_wall_timeout_for_session=lambda session_key: runner_wall_llm_timeout_s(
                self.sessions,
                session_key,
            ),
            max_concurrent_subagents=max_concurrent_subagents,
            subagent_timeout_s=subagent_timeout_s,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connect_lock = asyncio.Lock()
        self._mcp_connected_servers: set[str] = set()
        self._mcp_tool_names: set[str] = set()
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: set[asyncio.Task] = set()
        self._processing_lock = asyncio.Lock()
        self._exec_session_manager = ExecSessionManager()
        self.auto_compact = AutoCompact(
            self.sessions,
            _LoopIdleConsolidator(self),
            session_ttl_minutes=self.compaction_config.session_ttl_minutes,
        )
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        self._register_default_tools()

    def _build_memory_backend(self, config: "MemoryConfig") -> MemoryBackend:
        """Create the configured memory backend."""
        if config.backend == "core":
            return CoreMemoryBackend(
                self.workspace,
                daily_pages=config.daily_pages,
                recent_days=config.recent_days,
                extra_paths=config.extra_paths,
                search_max_results=config.search_max_results,
            )
        return LegacyMemoryBackend(self.workspace)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ApplyPatchTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
            session_manager=self._exec_session_manager,
        ))
        self.tools.register(WriteStdinTool(manager=self._exec_session_manager))
        self.tools.register(ListExecSessionsTool(manager=self._exec_session_manager))
        self.tools.register(
            WebSearchTool(
                api_key=self.brave_api_key,
                max_results=self.web_search_max_results,
                proxy=self.web_proxy,
                provider=self.web_search_provider,
                base_url=self.web_search_base_url,
            )
        )
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MemorySearchTool(workspace=self.workspace, backend=self.memory_backend))
        self.tools.register(MemoryGetTool(workspace=self.workspace, backend=self.memory_backend))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(LongTaskTool(self.sessions))
        self.tools.register(CompleteGoalTool(self.sessions))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.image_generation_config.enabled:
            self.tools.register(
                ImageGenerationTool(
                    workspace=self.workspace,
                    config=self.image_generation_config,
                    provider_configs=self.image_generation_provider_configs,
                )
            )
        if self.cli_apps_config.enabled:
            self.tools.register(
                CliAppsTool(
                    workspace=self.workspace,
                    timeout=self.cli_apps_config.run_timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                )
            )
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect configured MCP servers, retrying only servers that previously failed."""
        if not self._mcp_servers:
            return
        from yuanclaw.agent.tools.mcp import connect_mcp_servers

        async with self._mcp_connect_lock:
            pending = {
                name: cfg
                for name, cfg in self._mcp_servers.items()
                if name not in self._mcp_connected_servers
            }
            if not pending:
                self._mcp_connected = True
                return

            if self._mcp_stack is None:
                self._mcp_stack = AsyncExitStack()
                await self._mcp_stack.__aenter__()

            try:
                results = await connect_mcp_servers(pending, self.tools, self._mcp_stack)
            except Exception as exc:
                self._mcp_connected = False
                logger.error(
                    "Failed to connect MCP servers; pending servers will retry ({})",
                    type(exc).__name__,
                )
                return

            for name, result in results.items():
                if result.connected:
                    self._mcp_connected_servers.add(name)
                    self._mcp_tool_names.update(result.tool_names)

            self._mcp_connected = (
                self._mcp_connected_servers == set(self._mcp_servers)
            )

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in (
            "message",
            "spawn",
            "cron",
            "long_task",
            "complete_goal",
            "exec",
            "write_stdin",
            "list_exec_sessions",
        ):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    if name == "message":
                        tool.set_context(channel, chat_id, message_id, metadata)
                    else:
                        tool.set_context(channel, chat_id)

    @staticmethod
    def _copy_reply_routing(msg: InboundMessage, response: OutboundMessage) -> None:
        for key in ("message_id", "message_thread_id", "thread_ts"):
            value = msg.metadata.get(key)
            if value is not None:
                response.metadata.setdefault(key, value)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return strip_think(text) or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _progress_accepts_kwarg(callback: Callable[..., Any], name: str) -> bool:
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return False
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            return True
        return name in signature.parameters

    @classmethod
    async def _invoke_progress(
        cls,
        callback: Callable[..., Awaitable[None]],
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
    ) -> None:
        has_structured_events = bool(tool_events or file_edit_events)
        if has_structured_events:
            kwargs: dict[str, Any] = {"tool_hint": tool_hint}
            if cls._progress_accepts_kwarg(callback, "tool_events"):
                kwargs["tool_events"] = tool_events
            if cls._progress_accepts_kwarg(callback, "file_edit_events"):
                kwargs["file_edit_events"] = file_edit_events
            if len(kwargs) > 1:
                await callback(content, **kwargs)
                return
            if not content:
                return
        await callback(content, tool_hint=tool_hint)

    @staticmethod
    def _tool_event_start(tool_call: Any) -> dict[str, Any]:
        return {
            "version": 1,
            "phase": "start",
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": getattr(tool_call, "arguments", {}) or {},
            "result": None,
            "error": None,
            "files": [],
            "embeds": [],
        }

    @classmethod
    def _tool_event_finish(
        cls,
        tool_call: Any,
        execution: ToolExecutionResult,
    ) -> dict[str, Any]:
        start = cls._tool_event_start(tool_call)
        result = execution.content
        start["phase"] = "end" if execution.ok else "error"
        start["result"] = result if execution.ok else None
        start["error"] = execution.error
        if isinstance(result, dict):
            start["files"] = result.get("files") if isinstance(result.get("files"), list) else []
            start["embeds"] = result.get("embeds") if isinstance(result.get("embeds"), list) else []
        return start

    @staticmethod
    def _file_edit_events(
        tool_call: Any,
        *,
        phase: str,
        execution: ToolExecutionResult | None = None,
    ) -> list[dict[str, Any]]:
        if getattr(tool_call, "name", "") != "apply_patch":
            return []
        arguments = getattr(tool_call, "arguments", {}) or {}
        edits = arguments.get("edits") if isinstance(arguments, dict) else None
        if not isinstance(edits, list):
            return []
        effective_phase = "error" if phase == "end" and execution and not execution.ok else phase
        events: list[dict[str, Any]] = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            path = edit.get("path")
            action = edit.get("action")
            if not isinstance(path, str) or not isinstance(action, str):
                continue
            events.append(
                {
                    "version": 1,
                    "phase": effective_phase,
                    "call_id": str(getattr(tool_call, "id", "") or ""),
                    "tool": "apply_patch",
                    "path": path,
                    "action": action,
                    "error": execution.error if effective_phase == "error" and execution else None,
                }
            )
        return events

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Get or create a per-session dispatch lock."""
        lock = self._session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_key] = lock
        return lock

    def _register_active_task(self, session_key: str, task: asyncio.Task) -> None:
        tasks = self._active_tasks.setdefault(session_key, [])
        if task not in tasks:
            tasks.append(task)

    def _unregister_active_task(self, session_key: str, task: asyncio.Task) -> None:
        tasks = self._active_tasks.get(session_key)
        if tasks is not None and task in tasks:
            tasks.remove(task)
        if not tasks:
            self._active_tasks.pop(session_key, None)
            lock = self._session_locks.get(session_key)
            if lock is not None and not lock.locked():
                self._session_locks.pop(session_key, None)

    def _schedule_background(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.create_task(awaitable)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _check_idle_sessions(self, *extra_active_keys: str) -> None:
        active_keys = {
            key
            for key, tasks in self._active_tasks.items()
            if any(not task.done() for task in tasks)
        }
        active_keys.update(key for key in extra_active_keys if key)
        self.auto_compact.check_expired(self._schedule_background, active_session_keys=active_keys)

    @staticmethod
    def _parse_command(content: str) -> tuple[str, str] | None:
        """Parse a slash command into (command, args)."""
        raw = content.strip()
        if not raw.startswith("/"):
            return None
        cmd, _, args = raw.partition(" ")
        return cmd, args.strip()

    def _build_command_context(self, msg: InboundMessage, session: Session, key: str) -> CommandContext:
        """Build a command execution context for the current session."""
        parsed = self._parse_command(msg.content) or ("", "")
        return CommandContext(
            msg=msg,
            session=session,
            key=key,
            raw=parsed[0],
            args=parsed[1],
            loop=self,
        )

    def _build_memory_context(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        metadata: dict | None = None,
    ) -> str:
        """Build scoped memory context for the current turn."""
        return self.memory_backend.build_context(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            is_group=bool((metadata or {}).get("is_group", False)),
        )

    async def _dispatch_command(self, msg: InboundMessage, session: Session, key: str) -> OutboundMessage | None:
        """Dispatch a built-in command, handling priority commands immediately."""
        ctx = self._build_command_context(msg, session, key)
        if self.commands.is_priority(ctx.raw):
            return await self.commands.dispatch_priority(ctx)
        return await self.commands.dispatch(ctx)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        goal_active_predicate: Callable[[], bool] | None = None,
        goal_continue_message: str | None = None,
        llm_timeout_s: float | None = None,
    ) -> tuple[str | None, list[str], list[dict], dict[str, int]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages, usage)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        empty_content_retries = 0
        length_recovery_count = 0
        length_recovered_content = ""
        usage_totals = {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()
            if on_stream and hasattr(self.provider, "chat_stream"):
                response = await self._await_llm_response(
                    self.provider.chat_stream(  # type: ignore[attr-defined]
                        messages=messages,
                        tools=tool_defs,
                        model=self.model,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                        on_content_delta=on_stream,
                    ),
                    llm_timeout_s=llm_timeout_s,
                    streaming=True,
                )
            else:
                streamed_via_chat = False

                async def _on_chat_delta(text: str) -> None:
                    nonlocal streamed_via_chat
                    streamed_via_chat = True
                    if on_stream:
                        await on_stream(text)

                response = await self._await_llm_response(
                    self.provider.chat(
                        messages=messages,
                        tools=tool_defs,
                        model=self.model,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                        on_text_delta=_on_chat_delta if on_stream else None,
                    ),
                    llm_timeout_s=llm_timeout_s,
                    streaming=bool(on_stream),
                )
                if on_stream and response.content and not streamed_via_chat:
                    await on_stream(response.content)

            usage = response.usage or {}
            usage_totals["requests"] += 1
            usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            usage_totals["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            usage_totals["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
            self._last_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }

            if response.should_execute_tools:
                if on_stream and on_stream_end:
                    await on_stream_end(resuming=True)
                if on_progress:
                    if not on_stream:
                        thought = self._strip_think(response.content)
                        if thought:
                            await self._invoke_progress(on_progress, thought)
                    tool_hint = self._strip_think(self._tool_hint(response.tool_calls))
                    await self._invoke_progress(
                        on_progress,
                        tool_hint,
                        tool_hint=True,
                        tool_events=[
                            self._tool_event_start(tool_call)
                            for tool_call in response.tool_calls
                        ],
                        file_edit_events=[
                            event
                            for tool_call in response.tool_calls
                            for event in self._file_edit_events(tool_call, phase="start")
                        ],
                    )

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    usage=usage or None,
                    model=self.model,
                    provider=self.provider_name,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    execution = await self.tools.execute_result(
                        tool_call.name,
                        tool_call.arguments,
                    )
                    result = execution.content
                    if on_progress:
                        await self._invoke_progress(
                            on_progress,
                            "",
                            tool_events=[self._tool_event_finish(tool_call, execution)],
                            file_edit_events=self._file_edit_events(
                                tool_call,
                                phase="end",
                                execution=execution,
                            ),
                        )
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                if on_stream and on_stream_end:
                    await on_stream_end(resuming=False)
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                if not (clean or "").strip():
                    empty_content_retries += 1
                    if empty_content_retries < 2:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous response was an empty response. Please provide a non-empty "
                                    "answer to the user's request."
                                ),
                            }
                        )
                        final_content = None
                        continue
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    usage=usage or None,
                    model=self.model,
                    provider=self.provider_name,
                )
                if response.finish_reason == "length" and (clean or "").strip():
                    empty_content_retries = 0
                    length_recovery_count += 1
                    length_recovered_content += response.content or clean or ""
                    if length_recovery_count <= 2:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Continue exactly where you left off. Do not restart or repeat "
                                    "previous content."
                                ),
                            }
                        )
                        final_content = None
                        continue

                final_content = (
                    f"{length_recovered_content}{response.content or clean or ''}"
                    if length_recovered_content
                    else clean
                )
                if goal_active_predicate is not None and goal_active_predicate():
                    messages.append(
                        {
                            "role": "user",
                            "content": goal_continue_message
                            or (
                                "You still have an active sustained goal. Continue working toward it "
                                "using tools or call complete_goal if the objective is fully verified."
                            ),
                        }
                    )
                    final_content = None
                    continue
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
            messages = self.context.add_assistant_message(
                messages,
                final_content,
                model=self.model,
                provider=self.provider_name,
            )

        return final_content, tools_used, messages, usage_totals

    async def _await_llm_response(
        self,
        coro: Awaitable[LLMResponse],
        *,
        llm_timeout_s: float | None,
        streaming: bool,
    ) -> LLMResponse:
        """Await an LLM call with the runner wall timeout when applicable."""
        timeout_s = self.llm_timeout_s if llm_timeout_s is None else llm_timeout_s
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None
        try:
            if timeout_s is None:
                return await coro
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            return LLMResponse(
                content=f"Error calling LLM: timed out after {timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
                error_should_retry=True,
            )

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self._check_idle_sessions()
                continue
            except asyncio.CancelledError:
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result is not None:
                    await self.bus.publish_outbound(result)
                continue

            task = asyncio.create_task(self._dispatch(msg))
            self._register_active_task(msg.session_key, task)
            task.add_done_callback(
                lambda completed, key=msg.session_key: self._unregister_active_task(key, completed)
            )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        total = await self.cancel_session(msg.session_key)
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def cancel_session(self, session_key: str) -> int:
        """Cancel every tracked task and subprocess owned by one session."""
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._active_tasks.get(session_key, [])
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        subagent_count = await self.subagents.cancel_by_session(session_key)
        exec_count = await self._exec_session_manager.terminate_owner(session_key)
        return len(tasks) + subagent_count + exec_count

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message with per-session locking."""
        try:
            key = msg.session_key
            async with self._get_session_lock(key):
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):
                    base_meta = dict(msg.metadata or {})

                    async def on_stream(delta: str) -> None:
                        meta = {**base_meta, "_stream_delta": True}
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=delta,
                            metadata=meta,
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        meta = {**base_meta, "_stream_end": True, "_resuming": resuming}
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=meta,
                        ))

                response = await self._process_message(
                    msg,
                    session_key=key,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                )

            if response is not None:
                await self.bus.publish_outbound(response)
            elif msg.channel == "cli":
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="", metadata=msg.metadata or {},
                ))
        except asyncio.CancelledError:
            logger.info("Task cancelled for session {}", msg.session_key)
            raise
        except Exception:
            logger.exception("Error processing message for session {}", msg.session_key)
            if msg.metadata.get("_wants_stream") and callable(on_stream_end):
                try:
                    await on_stream_end(resuming=False)
                except Exception:
                    pass
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="Sorry, I encountered an error.",
            ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        async with self._mcp_connect_lock:
            stack = self._mcp_stack
            self._mcp_stack = None
            self._mcp_connected = False
            self._mcp_connected_servers.clear()
            tool_names = tuple(self._mcp_tool_names)
            self._mcp_tool_names.clear()
            for name in tool_names:
                self.tools.unregister(name)

        if stack:
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def shutdown(self, *, close_provider: bool = True) -> None:
        """Stop accepting work and await cleanup of all owned async resources."""
        self.stop()
        current = asyncio.current_task()
        tasks = {
            task
            for session_tasks in self._active_tasks.values()
            for task in session_tasks
            if task is not current and not task.done()
        }
        tasks.update(
            task
            for task in self._background_tasks | self._consolidation_tasks
            if task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()
        self._background_tasks.clear()
        self._consolidation_tasks.clear()
        self._consolidating.clear()
        self._session_locks.clear()
        await self.subagents.cancel_all()
        await self._exec_session_manager.terminate_all()
        await self.close_mcp()
        if close_provider:
            await self.provider.aclose()

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            session, pending_summary = self.auto_compact.prepare_session(session, key)
            self._set_tool_context(
                channel,
                chat_id,
                msg.metadata.get("message_id"),
                msg.metadata,
            )
            history = session.get_history(max_messages=self.memory_window)
            memory_context = self._build_memory_context(
                session_key=key,
                channel=channel,
                chat_id=chat_id,
                metadata=msg.metadata,
            )
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                memory_context=memory_context,
                session_metadata=session.metadata,
                session_summary=pending_summary,
            )
            final_content, _, all_msgs, usage = await self._run_agent_loop(
                messages,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                goal_active_predicate=lambda: sustained_goal_active(session.metadata),
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    key,
                    metadata=session.metadata,
                ),
            )
            self.sessions.record_usage(
                session,
                provider=self.provider_name,
                model=self.model,
                usage=usage,
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        self._check_idle_sessions(key)
        session = self.sessions.get_or_create(key)
        session, pending_summary = self.auto_compact.prepare_session(session, key)
        self._persist_runtime_attachments(session, msg.metadata)
        workspace_scope = self.workspace_scope_resolver.for_message(msg, session.metadata)
        self.workspace_scope_resolver.persist_message_scope(session, msg)
        scope_token = bind_workspace_scope(workspace_scope)
        try:
            return await self._process_message_in_scope(
                msg,
                session,
                key,
                pending_summary=pending_summary,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
            )
        finally:
            reset_workspace_scope(scope_token)

    async def _process_message_in_scope(
        self,
        msg: InboundMessage,
        session: Session,
        key: str,
        pending_summary: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a normal channel message with workspace scope already bound."""
        parsed = self._parse_command(msg.content)
        if parsed:
            response = await self._dispatch_command(msg, session, key)
            if response is not None:
                self._copy_reply_routing(msg, response)
                return response

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    try:
                        _task = asyncio.current_task()
                    except RuntimeError:
                        _task = None
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            msg.metadata.get("message_id"),
            msg.metadata,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        skill_names = msg.metadata.get("skill_names")
        if not isinstance(skill_names, list):
            skill_names = None
        if skill_names is None:
            fixed_skills = get_fixed_skills_for_session(key)
            skill_names = fixed_skills or None

        history = session.get_history(max_messages=self.memory_window)
        memory_context = self._build_memory_context(
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata,
        )
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=skill_names,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            memory_context=memory_context,
            session_metadata=session.metadata,
            session_summary=pending_summary,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, _, all_msgs, usage = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            goal_active_predicate=lambda: sustained_goal_active(session.metadata),
            llm_timeout_s=runner_wall_llm_timeout_s(
                self.sessions,
                key,
                metadata=session.metadata,
            ),
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self.sessions.record_usage(
            session,
            provider=self.provider_name,
            model=self.model,
            usage=usage,
        )
        self._save_turn(session, all_msgs, 1 + len(history), turn_metadata=msg.metadata)
        self.sessions.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        metadata = dict(msg.metadata or {})
        if on_stream is not None:
            metadata["_streamed"] = True

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=metadata,
        )

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        turn_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                entry["content"] = self._bounded_tool_result(content)
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
                if isinstance(turn_metadata, dict):
                    cli_apps = normalize_cli_app_mentions(
                        turn_metadata.get("cli_apps") or turn_metadata.get("cliApps")
                    )
                    mcp_presets = normalize_mcp_preset_mentions(
                        turn_metadata.get("mcp_presets") or turn_metadata.get("mcpPresets")
                    )
                    if cli_apps:
                        entry["cli_apps"] = cli_apps
                    if mcp_presets:
                        entry["mcp_presets"] = mcp_presets
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    def _bounded_tool_result(self, content: Any) -> str:
        """Serialize and bound a persisted tool result without splitting UTF-8 text."""
        if isinstance(content, str):
            serialized = content
        else:
            try:
                serialized = json.dumps(content, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                serialized = str(content)

        limit = self._TOOL_RESULT_MAX_BYTES
        encoded = serialized.encode("utf-8")
        if len(encoded) <= limit:
            return serialized

        suffix = self._TOOL_RESULT_TRUNCATION_SUFFIX.encode("utf-8")
        if limit <= len(suffix):
            return suffix[:limit].decode("utf-8", errors="ignore")
        prefix = encoded[:limit - len(suffix)].decode("utf-8", errors="ignore")
        return prefix + self._TOOL_RESULT_TRUNCATION_SUFFIX

    def _persist_runtime_attachments(self, session: Session, metadata: dict[str, Any]) -> None:
        cli_apps = normalize_cli_app_mentions(
            metadata.get("cli_apps") or metadata.get("cliApps")
        )
        mcp_presets = normalize_mcp_preset_mentions(
            metadata.get("mcp_presets") or metadata.get("mcpPresets")
        )
        if cli_apps:
            metadata["cli_apps"] = cli_apps
            session.metadata.update(cli_app_session_extra(metadata))
        if mcp_presets:
            metadata["mcp_presets"] = mcp_presets
            session.metadata.update(mcp_preset_session_extra(metadata))

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        consolidated = await MemoryStore(self.workspace).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )
        if consolidated:
            self.sessions.save(session)
        return consolidated

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        skill_names: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("process_direct requires an asyncio task")
        self._register_active_task(session_key, task)
        try:
            await self._connect_mcp()
            async with self._get_session_lock(session_key):
                metadata = dict(metadata or {})
                if skill_names:
                    metadata["skill_names"] = skill_names
                msg = InboundMessage(
                    channel=channel,
                    sender_id="user",
                    chat_id=chat_id,
                    content=content,
                    metadata=metadata,
                )
                response = await self._process_message(
                    msg,
                    session_key=session_key,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                )
            return response.content if response else ""
        finally:
            self._unregister_active_task(session_key, task)
