from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from yuanclaw.config.schema import Config
from yuanclaw.providers.anthropic_provider import AnthropicProvider
from yuanclaw.providers.factory import make_provider


class _FakeMessages:
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.stream_context: Any | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.stream_context


class _FakeAnthropicClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


class _FakeStream:
    def __init__(self, chunks: list[Any], final_message: Any) -> None:
        self._chunks = list(chunks)
        self._final_message = final_message

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def get_final_message(self) -> Any:
        return self._final_message


@dataclass
class _Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    thinking: str | None = None
    signature: str | None = None


def _response(*blocks: _Block, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=4,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=1,
        ),
    )


def test_make_provider_uses_native_anthropic_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AnthropicProvider, "_make_client", lambda self: object())
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "anthropic",
                    "model": "anthropic/claude-sonnet-4-5",
                }
            },
            "providers": {
                "anthropic": {
                    "apiKey": "anthropic-key",
                    "apiBase": "https://anthropic-proxy.test/v1",
                    "extraHeaders": {"anthropic-beta": "prompt-caching"},
                }
            },
        }
    )

    provider = make_provider(config)

    assert isinstance(provider, AnthropicProvider)
    assert provider.api_key == "anthropic-key"
    assert provider.api_base == "https://anthropic-proxy.test/v1"
    assert provider.normalized_api_base == "https://anthropic-proxy.test"
    assert provider.extra_headers == {"anthropic-beta": "prompt-caching"}


def test_anthropic_provider_builds_messages_tools_thinking_and_cache() -> None:
    provider = AnthropicProvider(
        api_key="key",
        default_model="anthropic/claude-sonnet-4-5",
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
            {"role": "user", "content": [{"foo": "bar"}, {"type": "text", "text": "again"}]},
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
        reasoning_effort="low",
        tool_choice="required",
    )

    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["max_tokens"] == 5120
    assert kwargs["temperature"] == 1.0
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert kwargs["system"] == [
        {"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}
    ]
    assert kwargs["tool_choice"] == {"type": "auto"}
    assert kwargs["tools"][0]["name"] == "lookup"
    assert kwargs["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["messages"][0] == {"role": "user", "content": "hi"}
    assert kwargs["messages"][1]["content"][1]["input"] == {"city": "Shanghai"}
    assert kwargs["messages"][2]["content"][0]["type"] == "tool_result"
    assert kwargs["messages"][2]["content"][1] == {"type": "text", "text": str({"foo": "bar"})}


def test_anthropic_provider_omits_temperature_for_opus_4_7() -> None:
    provider = AnthropicProvider(
        api_key="key",
        default_model="anthropic/claude-opus-4-7",
        client=object(),
    )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model=None,
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort="adaptive",
        tool_choice=None,
        supports_caching=False,
    )

    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "temperature" not in kwargs


def test_anthropic_provider_parses_response_usage_tools_and_thinking() -> None:
    response = AnthropicProvider._parse_response(
        _response(
            _Block(type="text", text="hello"),
            _Block(type="tool_use", id="call_1", name="lookup", input={"city": "Shanghai"}),
            _Block(type="thinking", thinking="thinking", signature="sig"),
            stop_reason="tool_use",
        )
    )

    assert response.content == "hello"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {
        "prompt_tokens": 6,
        "completion_tokens": 4,
        "total_tokens": 10,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 1,
        "cached_tokens": 1,
    }
    assert response.thinking_blocks == [
        {"type": "thinking", "thinking": "thinking", "signature": "sig"}
    ]
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"city": "Shanghai"}


@pytest.mark.asyncio
async def test_anthropic_provider_chat_falls_back_to_stream_when_required() -> None:
    messages = _FakeMessages(
        response=_response(_Block(type="text", text="fallback")),
        error=ValueError("Streaming is required for requests over 10 minutes"),
    )
    messages.stream_context = _FakeStream(
        chunks=[],
        final_message=_response(_Block(type="text", text="streamed")),
    )
    provider = AnthropicProvider(
        api_key="key",
        default_model="anthropic/claude-sonnet-4-5",
        client=_FakeAnthropicClient(messages),
    )

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "streamed"
    assert len(messages.calls) == 2


@pytest.mark.asyncio
async def test_anthropic_provider_stream_emits_text_thinking_and_tool_deltas() -> None:
    messages = _FakeMessages()
    messages.stream_context = _FakeStream(
        chunks=[
            SimpleNamespace(
                type="content_block_start",
                index=0,
                content_block=SimpleNamespace(type="tool_use", id="call_1", name="lookup"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="input_json_delta", partial_json="{\"city\""),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=1,
                delta=SimpleNamespace(type="thinking_delta", thinking="think"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=2,
                delta=SimpleNamespace(type="text_delta", text="hello"),
            ),
        ],
        final_message=_response(_Block(type="text", text="hello")),
    )
    provider = AnthropicProvider(
        api_key="key",
        default_model="anthropic/claude-sonnet-4-5",
        client=_FakeAnthropicClient(messages),
    )
    text_deltas: list[str] = []
    thinking_deltas: list[str] = []
    tool_deltas: list[dict[str, Any]] = []

    response = await provider.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        on_content_delta=lambda text: _append_text(text_deltas, text),
        on_thinking_delta=lambda text: _append_text(thinking_deltas, text),
        on_tool_call_delta=lambda delta: _append_tool_delta(tool_deltas, delta),
    )

    assert response.content == "hello"
    assert text_deltas == ["hello"]
    assert thinking_deltas == ["think"]
    assert tool_deltas == [
        {"index": 0, "call_id": "call_1", "name": "lookup", "arguments_delta": ""},
        {"index": 0, "call_id": "call_1", "name": "lookup", "arguments_delta": "{\"city\""},
    ]


async def _append_text(target: list[str], text: str) -> None:
    target.append(text)


async def _append_tool_delta(target: list[dict[str, Any]], delta: dict[str, Any]) -> None:
    target.append(delta)
