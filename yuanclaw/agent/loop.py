"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from yuanclaw.agent.context import ContextBuilder
from yuanclaw.agent.memory import MemoryStore
from yuanclaw.agent.subagent import SubagentManager
from yuanclaw.agent.tools.cron import CronTool
from yuanclaw.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from yuanclaw.agent.tools.message import MessageTool
from yuanclaw.agent.tools.registry import ToolRegistry
from yuanclaw.agent.tools.shell import ExecTool
from yuanclaw.agent.tools.spawn import SpawnTool
from yuanclaw.agent.tools.web import WebFetchTool, WebSearchTool
from yuanclaw.bus.events import InboundMessage, OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMProvider
from yuanclaw.session.manager import Session, SessionManager
from yuanclaw.studio_agents import get_fixed_skills_for_session

if TYPE_CHECKING:
    from yuanclaw.config.schema import ChannelsConfig, ExecToolConfig
    from yuanclaw.cron.service import CronService


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

    _TOOL_RESULT_MAX_CHARS = 500
    _TOOL_RESULT_PREVIEW_MAX_CHARS = 240

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
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from yuanclaw.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.provider_name = provider_name
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
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
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._processing_lock = asyncio.Lock()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from yuanclaw.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

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
    def _tool_result_is_success(result: str) -> bool:
        lowered = result.strip().lower()
        if not lowered:
            return False
        return not (
            lowered.startswith("error:")
            or lowered.startswith("(mcp tool call timed out")
            or lowered.startswith("(mcp tool call failed")
            or lowered.startswith("(mcp tool call was cancelled")
        )

    @staticmethod
    def _tool_result_failure_kind(result: str) -> str | None:
        lowered = result.strip().lower()
        if not lowered:
            return None
        if "timed out" in lowered or "timeout" in lowered:
            return "tool_timeout"
        if lowered.startswith("error:") or lowered.startswith("(mcp tool call failed"):
            return "tool_failed"
        return None

    @classmethod
    def _compact_tool_result(cls, result: str) -> str:
        compact = " ".join(str(result or "").split()).strip()
        if len(compact) <= cls._TOOL_RESULT_PREVIEW_MAX_CHARS:
            return compact
        return compact[: cls._TOOL_RESULT_PREVIEW_MAX_CHARS - 1].rstrip() + "…"

    @staticmethod
    def _looks_like_timeout_error(text: str | None) -> bool:
        lowered = str(text or "").strip().lower()
        return "timed out" in lowered or "timeout" in lowered

    @classmethod
    def _build_tool_timeout_degradation(
        cls,
        tool_name: str,
        tool_args: str,
        tool_result: str,
    ) -> str:
        tool_label = f"{tool_name}({tool_args})" if tool_args else tool_name
        summary = cls._compact_tool_result(tool_result)
        return (
            "工具已执行成功，但模型整理结果时超时。\n"
            f"最近完成的工具: {tool_label}\n"
            f"工具结果摘要: {summary}"
        )

    @classmethod
    def _build_tool_timeout_error(
        cls,
        tool_name: str,
        tool_args: str,
        tool_result: str,
    ) -> str:
        tool_label = f"{tool_name}({tool_args})" if tool_args else tool_name
        summary = cls._compact_tool_result(tool_result)
        return (
            "工具执行超时。\n"
            f"最近失败的工具: {tool_label}\n"
            f"工具输出摘要: {summary}"
        )

    @staticmethod
    def _build_llm_timeout_error() -> str:
        return "模型整理最终回复时超时，请重试。"

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        provider_name: str | None = None,
    ) -> tuple[str | None, list[str], list[dict], dict[str, int], dict[str, str]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages, usage, final_metadata)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        final_metadata: dict[str, str] = {}
        tools_used: list[str] = []
        usage_totals = {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        last_successful_tool: dict[str, str] | None = None
        last_failed_tool: dict[str, str] | None = None
        active_provider = provider or self.provider
        active_model = model or self.model
        active_provider_name = provider_name or self.provider_name

        while iteration < self.max_iterations:
            iteration += 1

            response = await active_provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=active_model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                on_text_delta=on_progress,
            )
            usage_totals["requests"] += 1
            usage_totals["prompt_tokens"] += int(response.usage.get("prompt_tokens", 0) or 0)
            usage_totals["completion_tokens"] += int(response.usage.get("completion_tokens", 0) or 0)
            usage_totals["total_tokens"] += int(response.usage.get("total_tokens", 0) or 0)

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

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
                    usage=response.usage or None,
                    model=active_model,
                    provider=active_provider_name,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    if isinstance(result, str) and self._tool_result_is_success(result):
                        last_successful_tool = {
                            "name": tool_call.name,
                            "arguments": args_str,
                            "result": result,
                        }
                        last_failed_tool = None
                    elif isinstance(result, str):
                        failure_kind = self._tool_result_failure_kind(result)
                        if failure_kind:
                            last_failed_tool = {
                                "name": tool_call.name,
                                "arguments": args_str,
                                "result": result,
                                "failure_kind": failure_kind,
                            }
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    if last_successful_tool and self._looks_like_timeout_error(clean):
                        logger.warning(
                            "Tool succeeded but LLM timed out while finalizing: {}({})",
                            last_successful_tool["name"],
                            last_successful_tool["arguments"][:200],
                        )
                        final_content = self._build_tool_timeout_degradation(
                            last_successful_tool["name"],
                            last_successful_tool["arguments"],
                            last_successful_tool["result"],
                        )
                        final_metadata = {
                            "_completion_kind": "degraded",
                            "_degraded_reason": "tool_succeeded_but_llm_timeout",
                        }
                    elif (
                        last_failed_tool
                        and last_failed_tool.get("failure_kind") == "tool_timeout"
                        and self._looks_like_timeout_error(clean)
                    ):
                        logger.warning(
                            "Tool timed out before final response: {}({})",
                            last_failed_tool["name"],
                            last_failed_tool["arguments"][:200],
                        )
                        final_content = self._build_tool_timeout_error(
                            last_failed_tool["name"],
                            last_failed_tool["arguments"],
                            last_failed_tool["result"],
                        )
                        final_metadata = {
                            "_completion_kind": "error",
                            "_error_kind": "tool_timeout",
                        }
                    elif self._looks_like_timeout_error(clean):
                        final_content = self._build_llm_timeout_error()
                        final_metadata = {
                            "_completion_kind": "error",
                            "_error_kind": "llm_timeout",
                        }
                    else:
                        final_content = clean or "Sorry, I encountered an error calling the AI model."
                        final_metadata = {
                            "_completion_kind": "error",
                            "_error_kind": "llm_provider_error",
                        }
                    break
                if not clean and not response.tool_calls:
                    logger.error(
                        "LLM returned an empty assistant message without tool calls (provider={}, model={}, finish_reason={})",
                        active_provider_name or "unknown",
                        active_model,
                        response.finish_reason,
                    )
                    final_content = (
                        "The configured model endpoint returned an empty assistant response. "
                        f"provider={active_provider_name or 'unknown'}, model={active_model}. "
                        "This usually means the upstream Chat Completions compatibility layer "
                        "accepted the request but did not generate message content."
                    )
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    usage=response.usage or None,
                    model=active_model,
                    provider=active_provider_name,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
            final_metadata = {
                "_completion_kind": "error",
                "_error_kind": "agent_max_iterations",
            }

        return final_content, tools_used, messages, usage_totals, final_metadata

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
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
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            final_content, _, all_msgs, usage, _ = await self._run_agent_loop(messages)
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
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 yuanclaw commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

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
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()
        if exec_tool := self.tools.get("exec"):
            if isinstance(exec_tool, ExecTool):
                exec_path_append = msg.metadata.get("exec_path_append")
                if isinstance(exec_path_append, list):
                    exec_tool.set_runtime_path_append(exec_path_append)
                else:
                    exec_tool.set_runtime_path_append(None)
        exec_commands = msg.metadata.get("exec_commands")
        if not isinstance(exec_commands, list):
            exec_commands = None

        skill_names = msg.metadata.get("skill_names")
        if not isinstance(skill_names, list):
            skill_names = None
        if skill_names is None:
            fixed_skills = get_fixed_skills_for_session(key)
            skill_names = fixed_skills or None
        skill_paths = msg.metadata.get("skill_paths")
        if not isinstance(skill_paths, list):
            skill_paths = None
        provider_override = msg.metadata.get("_provider_override")
        if not isinstance(provider_override, LLMProvider):
            provider_override = None
        model_override = msg.metadata.get("_model_override")
        if not isinstance(model_override, str) or not model_override.strip():
            model_override = None
        else:
            model_override = model_override.strip()
        provider_name_override = msg.metadata.get("_provider_name_override")
        if (
            not isinstance(provider_name_override, str)
            or not provider_name_override.strip()
        ):
            provider_name_override = None
        else:
            provider_name_override = provider_name_override.strip()
        active_provider = provider_override or self.provider
        active_model = model_override or self.model
        active_provider_name = provider_name_override or self.provider_name

        history = session.get_history(max_messages=self.memory_window)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=skill_names,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            exec_commands=exec_commands,
            skill_paths=skill_paths,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        original_subagent_provider = self.subagents.provider
        original_subagent_model = self.subagents.model
        self.subagents.provider = active_provider
        self.subagents.model = active_model
        try:
            final_content, _, all_msgs, usage, final_metadata = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress or _bus_progress,
                provider=active_provider,
                model=active_model,
                provider_name=active_provider_name,
            )
        finally:
            self.subagents.provider = original_subagent_provider
            self.subagents.model = original_subagent_model

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        if final_metadata.get("_completion_kind") == "degraded" and final_content:
            all_msgs = self.context.add_assistant_message(
                all_msgs,
                final_content,
                model=active_model,
                provider=active_provider_name,
            )
            if all_msgs and all_msgs[-1].get("role") == "assistant":
                all_msgs[-1]["completion_kind"] = "degraded"
                all_msgs[-1]["degraded_reason"] = final_metadata.get("_degraded_reason")

        self.sessions.record_usage(
            session,
            provider=active_provider_name,
            model=active_model,
            usage=usage,
        )
        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        response_metadata = dict(msg.metadata or {})
        response_metadata.update(final_metadata)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=response_metadata,
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
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
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        return await MemoryStore(self.workspace).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct_outbound(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        skill_names: list[str] | None = None,
        skill_paths: list[str] | None = None,
        exec_path_append: list[str] | None = None,
        exec_commands: list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        provider_override: LLMProvider | None = None,
        model_override: str | None = None,
        provider_name_override: str | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        metadata = {}
        if skill_names:
            metadata["skill_names"] = skill_names
        if skill_paths:
            metadata["skill_paths"] = skill_paths
        if exec_path_append:
            metadata["exec_path_append"] = exec_path_append
        if exec_commands:
            metadata["exec_commands"] = exec_commands
        if provider_override is not None:
            metadata["_provider_override"] = provider_override
        if model_override:
            metadata["_model_override"] = model_override
        if provider_name_override:
            metadata["_provider_name_override"] = provider_name_override
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata=metadata,
        )
        return await self._process_message(msg, session_key=session_key, on_progress=on_progress)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        skill_names: list[str] | None = None,
        skill_paths: list[str] | None = None,
        exec_path_append: list[str] | None = None,
        exec_commands: list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        provider_override: LLMProvider | None = None,
        model_override: str | None = None,
        provider_name_override: str | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        response = await self.process_direct_outbound(
            content=content,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            skill_names=skill_names,
            skill_paths=skill_paths,
            exec_path_append=exec_path_append,
            exec_commands=exec_commands,
            on_progress=on_progress,
            provider_override=provider_override,
            model_override=model_override,
            provider_name_override=provider_name_override,
        )
        return response.content if response else ""
