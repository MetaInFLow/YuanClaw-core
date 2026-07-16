from unittest.mock import MagicMock, patch

from yuanclaw.config.schema import Config
from yuanclaw.runtime import build_agent_loop


def test_runtime_factory_applies_canonical_capability_config(tmp_path) -> None:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.agents.defaults.model = "openai/gpt-test"
    config.tools.web.search.provider = "duckduckgo"
    config.tools.web.search.max_results = 7
    config.tools.cli_apps.enabled = True
    bus = MagicMock()
    provider = MagicMock()
    cron = MagicMock()
    sessions = MagicMock()
    restart_handler = MagicMock()

    with patch("yuanclaw.agent.loop.AgentLoop") as loop_cls:
        build_agent_loop(
            config,
            bus=bus,
            provider=provider,
            cron_service=cron,
            session_manager=sessions,
            restart_handler=restart_handler,
        )

    kwargs = loop_cls.call_args.kwargs
    assert kwargs["workspace"] == config.workspace_path
    assert kwargs["provider_name"] == config.get_provider_name(config.agents.defaults.model)
    assert kwargs["web_search_provider"] == "duckduckgo"
    assert kwargs["web_search_max_results"] == 7
    assert kwargs["cli_apps_config"] is config.tools.cli_apps
    assert kwargs["session_manager"] is sessions
    assert kwargs["restart_handler"] is restart_handler
