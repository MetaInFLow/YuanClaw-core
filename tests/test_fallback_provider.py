import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from yuanclaw.config.schema import Config
from yuanclaw.providers.base import LLMProvider, LLMResponse
from yuanclaw.providers.bedrock_provider import BedrockProvider
from yuanclaw.providers.factory import build_provider_snapshot, make_provider
from yuanclaw.providers.fallback_provider import FallbackProvider
from yuanclaw.providers.litellm_provider import LiteLLMProvider
from yuanclaw.providers.openai_compatible_provider import OpenAICompatibleProvider


class _FakeProvider(LLMProvider):
    def __init__(self, model: str, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.model = model
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return self.model

    async def aclose(self) -> None:
        self.closed = True


class _RaisingProvider(LLMProvider):
    def __init__(self, model: str, error: Exception, *, delta: str | None = None) -> None:
        super().__init__()
        self.model = model
        self.error = error
        self.delta = delta
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        if self.delta and kwargs.get("on_text_delta"):
            await kwargs["on_text_delta"](self.delta)
        raise self.error

    def get_default_model(self) -> str:
        return self.model


class _ConcurrentPrimary(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.entered = [asyncio.Event() for _ in responses]
        self.release = [asyncio.Event() for _ in responses]
        self.calls = 0

    async def chat(self, **kwargs: Any) -> LLMResponse:
        index = self.calls
        self.calls += 1
        self.entered[index].set()
        await self.release[index].wait()
        return self.responses[index]

    def get_default_model(self) -> str:
        return "primary-model"


class _FakeBedrockClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        }

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "stream": [
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "he"}}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "llo"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 2}}},
            ]
        }


@dataclass
class _FallbackPreset:
    model: str
    max_tokens: int = 1024
    temperature: float = 0.2
    reasoning_effort: str | None = None


@pytest.mark.asyncio
async def test_fallback_circuit_applies_concurrent_outcomes_in_attempt_order() -> None:
    primary = _ConcurrentPrimary([
        LLMResponse(
            content="temporary failure",
            finish_reason="error",
            error_kind="server_error",
        ),
        LLMResponse(content="newer success"),
    ])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model")],
        provider_factory=lambda _preset: _FakeProvider(
            "fallback-model",
            [LLMResponse(content="fallback success")],
        ),
    )

    older = asyncio.create_task(wrapper.chat(messages=[]))
    await primary.entered[0].wait()
    newer = asyncio.create_task(wrapper.chat(messages=[]))
    await primary.entered[1].wait()
    primary.release[1].set()
    assert (await newer).content == "newer success"

    primary.release[0].set()
    assert (await older).content == "fallback success"
    assert wrapper._primary_failures == 0
    assert wrapper._primary_tripped_at is None


@pytest.mark.asyncio
async def test_fallback_circuit_skips_primary_after_failure_threshold() -> None:
    error = LLMResponse(
        content="temporary failure",
        finish_reason="error",
        error_kind="server_error",
    )
    primary = _FakeProvider("primary-model", [error, error, error])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model")],
        provider_factory=lambda _preset: _FakeProvider(
            "fallback-model",
            [LLMResponse(content="fallback success")],
        ),
    )

    for _ in range(4):
        assert (await wrapper.chat(messages=[])).content == "fallback success"

    assert len(primary.calls) == 3
    assert wrapper._primary_tripped_at is not None


@pytest.mark.asyncio
async def test_fallback_provider_uses_next_model_for_retryable_primary_error() -> None:
    primary = _FakeProvider(
        "primary-model",
        [
            LLMResponse(
                content="rate limited",
                finish_reason="error",
                error_status_code=429,
                error_kind="rate_limit",
            )
        ],
    )
    fallback = _FakeProvider("fallback-model", [LLMResponse(content="fallback-ok")])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model", max_tokens=512, temperature=0.4)],
        provider_factory=lambda _preset: fallback,
    )

    response = await wrapper.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="primary-model",
        max_tokens=2048,
        temperature=0.1,
        reasoning_effort="high",
    )

    assert response.content == "fallback-ok"
    assert fallback.calls[0]["model"] == "fallback-model"
    assert fallback.calls[0]["max_tokens"] == 512
    assert fallback.calls[0]["temperature"] == 0.4
    assert "reasoning_effort" not in fallback.calls[0]
    assert fallback.closed is True


@pytest.mark.asyncio
async def test_fallback_provider_does_not_retry_authentication_errors() -> None:
    primary = _FakeProvider(
        "primary-model",
        [
            LLMResponse(
                content="bad key",
                finish_reason="error",
                error_status_code=401,
                error_kind="authentication",
                error_should_retry=False,
            )
        ],
    )
    fallback = _FakeProvider("fallback-model", [LLMResponse(content="should-not-run")])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model")],
        provider_factory=lambda _preset: fallback,
    )

    response = await wrapper.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "bad key"
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_fallback_provider_recovers_from_primary_exception() -> None:
    primary = _RaisingProvider("primary-model", ConnectionError("offline"))
    fallback = _FakeProvider("fallback-model", [LLMResponse(content="fallback-ok")])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model")],
        provider_factory=lambda _preset: fallback,
    )

    response = await wrapper.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "fallback-ok"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_fallback_provider_continues_after_fallback_exception() -> None:
    primary = _FakeProvider(
        "primary-model",
        [
            LLMResponse(
                content="unavailable",
                finish_reason="error",
                error_kind="server_error",
            )
        ],
    )
    first = _RaisingProvider("first", ConnectionError("offline"))
    second = _FakeProvider("second", [LLMResponse(content="second-ok")])
    providers = {"first": first, "second": second}
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[
            _FallbackPreset(model="first"),
            _FallbackPreset(model="second"),
        ],
        provider_factory=lambda preset: providers[preset.model],
    )

    response = await wrapper.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "second-ok"
    assert len(first.calls) == 1
    assert len(second.calls) == 1


@pytest.mark.asyncio
async def test_fallback_provider_does_not_retry_after_stream_output() -> None:
    primary = _RaisingProvider(
        "primary-model",
        ConnectionError("stream interrupted"),
        delta="partial",
    )
    fallback = _FakeProvider("fallback-model", [LLMResponse(content="duplicate")])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model")],
        provider_factory=lambda _preset: fallback,
    )
    deltas: list[str] = []

    async def on_delta(delta: str) -> None:
        deltas.append(delta)

    response = await wrapper.chat(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=on_delta,
    )

    assert deltas == ["partial"]
    assert response.finish_reason == "error"
    assert response.error_kind == "connection"
    assert fallback.calls == []


@pytest.mark.asyncio
async def test_fallback_provider_treats_structured_error_as_failure() -> None:
    primary = _FakeProvider(
        "primary-model",
        [LLMResponse(content="limited", error_kind="rate_limit")],
    )
    fallback = _FakeProvider("fallback-model", [LLMResponse(content="fallback-ok")])
    wrapper = FallbackProvider(
        primary=primary,
        fallback_presets=[_FallbackPreset(model="fallback-model")],
        provider_factory=lambda _preset: fallback,
    )

    response = await wrapper.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "fallback-ok"


@pytest.mark.asyncio
async def test_litellm_does_not_reissue_request_after_stream_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def interrupted_stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="partial", tool_calls=[]),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        raise ConnectionError("stream interrupted")

    async def fake_acompletion(**kwargs):
        nonlocal calls
        calls += 1
        return interrupted_stream()

    monkeypatch.setattr("yuanclaw.providers.litellm_provider.acompletion", fake_acompletion)
    provider = LiteLLMProvider(default_model="openai/test")
    deltas: list[str] = []

    async def on_delta(delta: str) -> None:
        deltas.append(delta)

    response = await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=on_delta,
    )

    assert calls == 1
    assert deltas == ["partial"]
    assert response.finish_reason == "error"
    assert response.error_kind == "connection"


@pytest.mark.asyncio
async def test_litellm_stream_keeps_usage_only_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def usage_stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="ok", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=2,
                completion_tokens=1,
                total_tokens=3,
            ),
        )

    async def fake_acompletion(**kwargs):
        return usage_stream()

    monkeypatch.setattr("yuanclaw.providers.litellm_provider.acompletion", fake_acompletion)
    provider = LiteLLMProvider(default_model="openai/test")

    response = await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=lambda _delta: _async_noop(),
    )

    assert response.content == "ok"
    assert response.usage == {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }


async def _async_noop() -> None:
    return None


def test_make_provider_wraps_configured_fallback_models() -> None:
    config = Config()
    config.agents.defaults.model = "openai/gpt-5"
    config.agents.defaults.provider = "openai"
    config.agents.defaults.fallback_models = [
        {
            "model": "moonshot/kimi-k2.5",
            "provider": "moonshot",
            "maxTokens": 1024,
            "temperature": 1.0,
        }
    ]
    config.providers.openai.api_key = "openai-key"
    config.providers.moonshot.api_key = "moonshot-key"

    provider = make_provider(config)

    assert isinstance(provider, FallbackProvider)
    assert provider.get_default_model() == "openai/gpt-5"


def test_provider_snapshot_uses_smallest_context_window_across_fallbacks() -> None:
    config = Config()
    config.agents.defaults.model = "openai/gpt-5"
    config.agents.defaults.provider = "openai"
    config.agents.defaults.context_window_tokens = 128_000
    config.agents.defaults.fallback_models = [
        {
            "model": "moonshot/kimi-k2.5",
            "provider": "moonshot",
            "contextWindowTokens": 64_000,
        }
    ]
    config.providers.openai.api_key = "openai-key"
    config.providers.moonshot.api_key = "moonshot-key"

    snapshot = build_provider_snapshot(config)

    assert isinstance(snapshot.provider, FallbackProvider)
    assert snapshot.model == "openai/gpt-5"
    assert snapshot.context_window_tokens == 64_000
    assert snapshot.signature[-1]


def test_config_exposes_new_upstream_provider_entries() -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "longcat", "model": "longcat/LongCat-Flash"}},
            "providers": {
                "longcat": {"apiKey": "longcat-key"},
                "novita": {"apiKey": "novita-key"},
                "stepfun": {"apiKey": "stepfun-key"},
                "bedrock": {"region": "us-east-1", "profile": "default"},
            },
        }
    )

    assert config.get_provider_name() == "longcat"
    assert config.get_api_key() == "longcat-key"
    assert config.get_api_base() == "https://api.longcat.chat/openai/v1"
    assert config.providers.novita.api_key == "novita-key"
    assert config.providers.stepfun.api_key == "stepfun-key"
    assert config.providers.bedrock.region == "us-east-1"


def test_config_accepts_fallback_models_from_payload() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "model": "openai/gpt-5",
                    "fallbackModels": [
                        {
                            "model": "stepfun/step-2-mini",
                            "provider": "stepfun",
                            "maxTokens": 512,
                            "contextWindowTokens": 32_000,
                        }
                    ],
                }
            }
        }
    )

    fallback = config.agents.defaults.fallback_models[0]
    assert fallback.model == "stepfun/step-2-mini"
    assert fallback.provider == "stepfun"
    assert fallback.max_tokens == 512
    assert fallback.context_window_tokens == 32_000


def test_make_provider_uses_native_openai_compatible_provider_for_longcat() -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "longcat", "model": "LongCat-Flash"}},
            "providers": {
                "longcat": {
                    "apiKey": "longcat-key",
                    "extraBody": {"enable_search": True},
                }
            },
        }
    )

    provider = make_provider(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_key == "longcat-key"
    assert provider.api_base == "https://api.longcat.chat/openai/v1"
    assert provider.extra_body == {"enable_search": True}


@pytest.mark.parametrize(
    ("provider_name", "model", "api_base", "strip_model_prefix"),
    [
        ("openrouter", "anthropic/claude-sonnet-4-5", "https://openrouter.ai/api/v1", False),
        ("volcengine", "doubao-seed-1-6", "https://ark.cn-beijing.volces.com/api/v3", False),
        ("byteplus", "byteplus/seed-1-6", "https://ark.ap-southeast.bytepluses.com/api/v3", True),
    ],
)
def test_make_provider_uses_native_openai_compatible_provider_for_gateways(
    provider_name: str,
    model: str,
    api_base: str,
    strip_model_prefix: bool,
) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": provider_name, "model": model}},
            "providers": {
                provider_name: {
                    "apiKey": f"{provider_name}-key",
                    "extraBody": {"trace": provider_name},
                }
            },
        }
    )

    provider = make_provider(config)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_key == f"{provider_name}-key"
    assert provider.api_base == api_base
    assert provider.provider_name == provider_name
    assert provider.extra_body == {"trace": provider_name}
    assert provider.strip_model_prefix is strip_model_prefix


def test_openai_compatible_provider_builds_request_with_extra_body_and_tools() -> None:
    provider = OpenAICompatibleProvider(
        api_key="key",
        api_base="https://example.test/v1",
        default_model="demo-model",
        provider_name="demo",
        extra_body={"trace_id": "abc"},
    )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi", "metadata": "drop"}],
        tools=[{"type": "function", "function": {"name": "ping", "parameters": {}}}],
        model=None,
        max_tokens=0,
        temperature=0.2,
        reasoning_effort="low",
        tool_choice=None,
    )

    assert kwargs["model"] == "demo-model"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["max_tokens"] == 1
    assert kwargs["temperature"] == 0.2
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["tools"] == [{"type": "function", "function": {"name": "ping", "parameters": {}}}]
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["extra_body"] == {"trace_id": "abc"}


def test_openai_compatible_provider_strips_model_prefix_when_configured() -> None:
    provider = OpenAICompatibleProvider(
        api_key="key",
        api_base="https://gateway.test/v1",
        default_model="anthropic/claude-sonnet-4-5",
        provider_name="gateway",
        strip_model_prefix=True,
    )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        max_tokens=10,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "claude-sonnet-4-5"


def test_make_provider_uses_native_bedrock_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BedrockProvider, "_make_client", lambda self: object())
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "bedrock",
                    "model": "bedrock/us.anthropic.claude-sonnet-4-5",
                }
            },
            "providers": {
                "bedrock": {
                    "apiKey": "bedrock-token",
                    "apiBase": "https://bedrock-runtime.test",
                    "region": "us-west-2",
                    "profile": "prod",
                    "extraBody": {"thinking": {"type": "adaptive"}},
                }
            },
        }
    )

    provider = make_provider(config)

    assert isinstance(provider, BedrockProvider)
    assert provider.api_key == "bedrock-token"
    assert provider.api_base == "https://bedrock-runtime.test"
    assert provider.region == "us-west-2"
    assert provider.profile == "prod"


def test_bedrock_provider_matches_region_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BedrockProvider, "_make_client", lambda self: object())
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "auto",
                    "model": "bedrock/us.anthropic.claude-sonnet-4-5",
                }
            },
            "providers": {"bedrock": {"region": "us-east-1", "profile": "dev"}},
        }
    )

    provider = make_provider(config)

    assert config.get_provider_name() == "bedrock"
    assert isinstance(provider, BedrockProvider)
    assert provider.api_key is None
    assert provider.region == "us-east-1"
    assert provider.profile == "dev"


def test_bedrock_provider_builds_converse_kwargs_with_tools_and_extra_body() -> None:
    provider = BedrockProvider(
        api_key="token",
        api_base="https://bedrock-runtime.test",
        default_model="bedrock/us.anthropic.claude-sonnet-4-5",
        region="us-east-1",
        profile="dev",
        extra_body={"guardrailConfig": {"trace": "enabled"}},
        client=object(),
    )

    kwargs = provider._build_kwargs(
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "lookup",
                            "arguments": "{\"city\":\"Shanghai\"}",
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": {"ok": True}},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "lookup city",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        model=None,
        max_tokens=0,
        temperature=0.2,
        reasoning_effort=None,
        tool_choice="required",
    )

    assert kwargs["modelId"] == "us.anthropic.claude-sonnet-4-5"
    assert kwargs["system"] == [{"text": "be brief"}]
    assert kwargs["inferenceConfig"] == {"maxTokens": 1, "temperature": 0.2}
    assert kwargs["additionalModelRequestFields"] == {"guardrailConfig": {"trace": "enabled"}}
    assert kwargs["toolConfig"]["toolChoice"] == {"any": {}}
    assert kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"] == "lookup"
    assert kwargs["messages"][0] == {"role": "user", "content": [{"text": "hi"}]}
    assert kwargs["messages"][1]["content"][1]["toolUse"]["input"] == {"city": "Shanghai"}
    assert kwargs["messages"][2]["content"][0]["toolResult"]["content"] == [{"json": {"ok": True}}]


def test_bedrock_provider_parses_converse_response() -> None:
    response = BedrockProvider._parse_response(
        {
            "output": {
                "message": {
                    "content": [
                        {"text": "hello"},
                        {
                            "toolUse": {
                                "toolUseId": "call_1",
                                "name": "lookup",
                                "input": {"city": "Shanghai"},
                            }
                        },
                        {
                            "reasoningContent": {
                                "reasoningText": {
                                    "text": "thinking",
                                    "signature": "sig",
                                }
                            }
                        },
                    ]
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
        }
    )

    assert response.content == "hello"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert response.reasoning_content == "thinking"
    assert response.thinking_blocks == [
        {"type": "thinking", "thinking": "thinking", "signature": "sig"}
    ]
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"city": "Shanghai"}


@pytest.mark.asyncio
async def test_bedrock_provider_chat_and_stream_use_converse_client() -> None:
    client = _FakeBedrockClient()
    provider = BedrockProvider(
        default_model="bedrock/us.anthropic.claude-sonnet-4-5",
        region="us-east-1",
        client=client,
    )

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])
    deltas: list[str] = []
    streamed = await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=lambda text: _append_delta(deltas, text),
    )

    assert response.content == "ok"
    assert streamed.content == "hello"
    assert streamed.usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert deltas == ["he", "llo"]
    assert client.calls[0]["modelId"] == "us.anthropic.claude-sonnet-4-5"
    assert client.calls[1]["modelId"] == "us.anthropic.claude-sonnet-4-5"


async def _append_delta(target: list[str], text: str) -> None:
    target.append(text)
