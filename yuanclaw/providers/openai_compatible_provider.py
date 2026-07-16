"""Native OpenAI-compatible provider used for upstream direct/gateway APIs."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import json_repair
from openai import AsyncOpenAI

from yuanclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})


class OpenAICompatibleProvider(LLMProvider):
    """Direct provider for APIs that implement OpenAI chat completions."""

    def __init__(
        self,
        api_key: str = "no-key",
        api_base: str = "http://localhost:8000/v1",
        default_model: str = "default",
        *,
        provider_name: str | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        strip_model_prefix: bool = False,
    ) -> None:
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.provider_name = provider_name
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.strip_model_prefix = strip_model_prefix
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers={"x-session-affinity": uuid.uuid4().hex, **self.extra_headers},
        )

    def _resolve_model(self, model: str | None) -> str:
        resolved = model or self.default_model
        if self.strip_model_prefix and "/" in resolved:
            return resolved.split("/", 1)[1]
        return resolved

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(model),
            "messages": self._sanitize_request_messages(
                self._sanitize_empty_content(messages),
                _ALLOWED_MSG_KEYS,
            ),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        return kwargs

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(
            messages,
            tools,
            model,
            max_tokens,
            temperature,
            reasoning_effort,
            tool_choice,
        )
        try:
            if on_text_delta is not None:
                return await self._chat_stream(kwargs, on_text_delta)
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as exc:
            return self._handle_error(exc)

    async def _chat_stream(
        self,
        kwargs: dict[str, Any],
        on_text_delta: Callable[[str], Awaitable[None]],
    ) -> LLMResponse:
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        stream = await self._client.chat.completions.create(**stream_kwargs)
        chunks: list[Any] = []
        async for chunk in stream:
            chunks.append(chunk)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                await on_text_delta(text)
        return self._parse_chunks(chunks)

    def _handle_error(self, exc: Exception) -> LLMResponse:
        body = getattr(exc, "doc", None) or getattr(getattr(exc, "response", None), "text", None)
        message = f"Error: {body.strip()[:500]}" if body and body.strip() else f"Error: {exc}"
        return LLMResponse(content=message, finish_reason="error")

    def _parse(self, response: Any) -> LLMResponse:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return LLMResponse(content="Error: API returned empty choices.", finish_reason="error")
        choice = choices[0]
        msg = choice.message
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=getattr(msg, "content", None),
            tool_calls=self._parse_tool_calls(getattr(msg, "tool_calls", None) or []),
            finish_reason=getattr(choice, "finish_reason", None) or "stop",
            usage=self._parse_usage(usage),
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def _parse_chunks(self, chunks: list[Any]) -> LLMResponse:
        content_parts: list[str] = []
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        for chunk in chunks:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = self._parse_usage(chunk_usage)
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
            for tool_call in getattr(delta, "tool_calls", None) or []:
                index = getattr(tool_call, "index", None)
                if index is None:
                    index = len(tool_call_buffers)
                buffer = tool_call_buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if getattr(tool_call, "id", None):
                    buffer["id"] = tool_call.id
                function = getattr(tool_call, "function", None)
                if function is None:
                    continue
                if getattr(function, "name", None):
                    buffer["name"] = function.name
                if getattr(function, "arguments", None):
                    buffer["arguments"] += function.arguments

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=[
                ToolCallRequest(
                    id=str(buffer["id"]),
                    name=str(buffer["name"]),
                    arguments=(
                        json_repair.loads(buffer["arguments"])
                        if buffer["arguments"]
                        else {}
                    ),
                )
                for _, buffer in sorted(tool_call_buffers.items())
            ],
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _parse_tool_calls(tool_calls: list[Any]) -> list[ToolCallRequest]:
        parsed = []
        for tool_call in tool_calls:
            function = getattr(tool_call, "function", None)
            if function is None:
                continue
            arguments = getattr(function, "arguments", {}) or {}
            parsed.append(
                ToolCallRequest(
                    id=getattr(tool_call, "id", ""),
                    name=getattr(function, "name", ""),
                    arguments=json_repair.loads(arguments) if isinstance(arguments, str) else arguments,
                )
            )
        return parsed

    @staticmethod
    def _parse_usage(usage: Any) -> dict[str, int]:
        if not usage:
            return {}
        return {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }

    def get_default_model(self) -> str:
        return self.default_model

    async def aclose(self) -> None:
        await self._close_resource(self._client)
