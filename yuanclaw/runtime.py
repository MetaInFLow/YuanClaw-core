"""Shared runtime component construction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from yuanclaw.config.schema import Config
from yuanclaw.providers.image_generation import image_gen_provider_configs

if TYPE_CHECKING:
    from yuanclaw.agent.loop import AgentLoop
    from yuanclaw.bus.queue import MessageBus
    from yuanclaw.cron.service import CronService
    from yuanclaw.providers.base import LLMProvider
    from yuanclaw.session.manager import SessionManager


def build_agent_loop(
    config: Config,
    *,
    bus: MessageBus,
    provider: LLMProvider,
    cron_service: CronService,
    session_manager: SessionManager | None = None,
    restart_handler: Callable[[], bool | Awaitable[bool]] | None = None,
) -> AgentLoop:
    """Construct AgentLoop from one canonical Config-to-runtime mapping."""
    from yuanclaw.agent.loop import AgentLoop

    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        provider_name=config.get_provider_name(config.agents.defaults.model),
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        memory_config=config.agents.defaults.memory,
        compaction_config=config.agents.defaults.compaction,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_search_provider=config.tools.web.search.provider,
        web_search_base_url=config.tools.web.search.base_url or None,
        web_search_max_results=config.tools.web.search.max_results,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron_service,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        image_generation_config=config.tools.image_generation,
        image_generation_provider_configs=image_gen_provider_configs(config),
        cli_apps_config=config.tools.cli_apps,
        max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
        subagent_timeout_s=config.agents.defaults.subagent_timeout_s,
        restart_handler=restart_handler,
    )
