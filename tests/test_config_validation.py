import pytest
from pydantic import ValidationError

from yuanclaw.config.schema import (
    AgentDefaults,
    CompactionConfig,
    ExecToolConfig,
    GatewayConfig,
    GenerationConfig,
    InlineFallbackConfig,
    MemoryConfig,
    MemoryFlushConfig,
    WebSearchConfig,
)


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (MemoryConfig, {"recent_days": -1}),
        (MemoryConfig, {"search_max_results": 0}),
        (MemoryFlushConfig, {"soft_threshold_tokens": 0}),
        (CompactionConfig, {"reserve_tokens_floor": -1}),
        (GenerationConfig, {"max_tokens": 0}),
        (GenerationConfig, {"context_window_tokens": 0}),
        (GenerationConfig, {"temperature": -0.1}),
        (GenerationConfig, {"temperature": 2.1}),
        (InlineFallbackConfig, {"model": "fallback", "max_tokens": 0}),
        (InlineFallbackConfig, {"model": "fallback", "context_window_tokens": 0}),
        (InlineFallbackConfig, {"model": "fallback", "temperature": 2.1}),
        (AgentDefaults, {"max_tool_iterations": 0}),
        (AgentDefaults, {"max_concurrent_subagents": 101}),
        (AgentDefaults, {"memory_window": 1}),
        (GatewayConfig, {"port": 0}),
        (GatewayConfig, {"port": 65536}),
        (WebSearchConfig, {"provider": "unsupported"}),
        (WebSearchConfig, {"max_results": 11}),
        (ExecToolConfig, {"timeout": 0}),
    ],
)
def test_runtime_config_rejects_invalid_bounds(model, values) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_runtime_config_keeps_supported_disable_values_and_aliases() -> None:
    memory = MemoryConfig(recent_days=0)
    compaction = CompactionConfig.model_validate(
        {"reserveTokensFloor": 0, "sessionTtlMinutes": 0}
    )

    assert memory.recent_days == 0
    assert compaction.reserve_tokens_floor == 0
    assert compaction.session_ttl_minutes == 0
