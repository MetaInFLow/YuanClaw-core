"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import json_repair
from openai import AsyncOpenAI

from yuanclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class CustomProvider(LLMProvider):

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        adapter: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.adapter = (adapter or "openai_chat_stream_aggregate").strip() or "openai_chat_stream_aggregate"
        # Keep affinity stable for this provider instance to improve backend cache locality.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers={"x-session-affinity": uuid.uuid4().hex},
        )

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   on_text_delta: Callable[[str], Awaitable[None]] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        try:
            if self.adapter == "openai_chat":
                return self._parse(await self._client.chat.completions.create(**kwargs))
            if self.adapter == "openai_chat_stream_aggregate":
                return await self._parse_streaming_response(kwargs, on_text_delta=on_text_delta)
            return LLMResponse(
                content=f"Error: unsupported custom provider adapter `{self.adapter}`",
                finish_reason="error",
            )
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCallRequest(id=tc.id, name=tc.function.name,
                            arguments=json_repair.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        u = response.usage
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    async def _parse_streaming_response(
        self,
        kwargs: dict[str, Any],
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        stream = await self._client.chat.completions.create(stream=True, **kwargs)
        text_parts: list[str] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}
        tool_calls_by_index: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"id": "", "name": "", "arguments_parts": []}
        )

        async for chunk in stream:
            choice = (chunk.choices or [None])[0]
            if choice is None:
                continue
            delta = choice.delta
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    text_parts.append(content)
                    if on_text_delta:
                        await on_text_delta(content)

                for tool_call in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(tool_call, "index", 0) or 0)
                    bucket = tool_calls_by_index[index]
                    if getattr(tool_call, "id", None):
                        bucket["id"] = tool_call.id
                    fn = getattr(tool_call, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            bucket["name"] = fn.name
                        arguments = getattr(fn, "arguments", None)
                        if arguments:
                            bucket["arguments_parts"].append(arguments)

            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason or finish_reason

            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = {
                    "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(chunk_usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(chunk_usage, "total_tokens", 0) or 0,
                }

        tool_calls = []
        for bucket in tool_calls_by_index.values():
            raw_arguments = "".join(bucket["arguments_parts"]).strip() or "{}"
            try:
                parsed_arguments = (
                    json_repair.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
            except Exception:
                parsed_arguments = json.loads(raw_arguments)
            tool_calls.append(
                ToolCallRequest(
                    id=bucket["id"] or uuid.uuid4().hex[:8],
                    name=bucket["name"],
                    arguments=parsed_arguments,
                )
            )

        return LLMResponse(
            content="".join(text_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def get_default_model(self) -> str:
        return self.default_model
