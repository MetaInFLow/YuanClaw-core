"""Native Anthropic Messages API provider."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import string
from collections.abc import Awaitable, Callable
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import json_repair

from yuanclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_ALNUM = string.ascii_letters + string.digits


def _gen_tool_id() -> str:
    return "toolu_" + "".join(secrets.choice(_ALNUM) for _ in range(22))


def _tool_cache_marker_indices(tools: list[dict[str, Any]]) -> list[int]:
    if not tools:
        return []
    # Anthropic allows up to four cache breakpoints. Marking the last tools is
    # the most useful approximation for dynamic tool lists.
    return list(range(max(0, len(tools) - 4), len(tools)))


def _retry_after_seconds(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
        return max(0.0, target.timestamp() - datetime.now(target.tzinfo).timestamp())
    except Exception:
        return None


def _error_type_code(payload: Any) -> tuple[str | None, str | None]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return _string_or_none(error.get("type")), _string_or_none(error.get("code"))
        return _string_or_none(payload.get("type")), _string_or_none(payload.get("code"))
    if isinstance(payload, str):
        try:
            return _error_type_code(json.loads(payload))
        except Exception:
            return None, None
    return None, None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class AnthropicProvider(LLMProvider):
    """Provider backed by Anthropic's native Messages API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-sonnet-4-5",
        extra_headers: dict[str, str] | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.normalized_api_base = self._normalize_base_url(api_base) if api_base else None
        self._client = client if client is not None else self._make_client()

    def _make_client(self) -> Any:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dep
            raise RuntimeError(
                "Anthropic provider requires anthropic. Install it with `pip install anthropic`."
            ) from exc

        client_kwargs: dict[str, Any] = {"max_retries": 0}
        if self.api_key:
            client_kwargs["api_key"] = self.api_key
        if self.normalized_api_base:
            client_kwargs["base_url"] = self.normalized_api_base
        if self.extra_headers:
            client_kwargs["default_headers"] = self.extra_headers
        return AsyncAnthropic(**client_kwargs)

    @staticmethod
    def _normalize_base_url(api_base: str) -> str:
        normalized = api_base.rstrip("/")
        if normalized.endswith("/v1"):
            return normalized[: -len("/v1")]
        return normalized

    @staticmethod
    def _strip_prefix(model: str) -> str:
        if model.startswith("anthropic/"):
            return model[len("anthropic/"):]
        return model

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]]]:
        system: str | list[dict[str, Any]] = ""
        raw: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")
            if role == "system":
                system = content if isinstance(content, (str, list)) else str(content or "")
                continue
            if role == "tool":
                block = self._tool_result_block(msg)
                if raw and raw[-1]["role"] == "user":
                    prev_content = raw[-1]["content"]
                    if isinstance(prev_content, list):
                        prev_content.append(block)
                    else:
                        raw[-1]["content"] = [
                            {"type": "text", "text": prev_content or ""},
                            block,
                        ]
                else:
                    raw.append({"role": "user", "content": [block]})
                continue
            if role == "assistant":
                raw.append({"role": "assistant", "content": self._assistant_blocks(msg)})
                continue
            if role == "user":
                raw.append({"role": "user", "content": self._convert_user_content(content)})

        return system, self._merge_consecutive(raw)

    @staticmethod
    def _tool_result_block(msg: dict[str, Any]) -> dict[str, Any]:
        content = msg.get("content")
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": msg.get("tool_call_id", ""),
        }
        if isinstance(content, list):
            block["content"] = AnthropicProvider._convert_user_content(content)
        elif isinstance(content, str):
            block["content"] = content
        else:
            block["content"] = str(content) if content else ""
        return block

    @staticmethod
    def _assistant_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        content = msg.get("content")

        for thinking in msg.get("thinking_blocks") or []:
            if isinstance(thinking, dict) and thinking.get("type") == "thinking":
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": thinking.get("thinking", ""),
                        "signature": thinking.get("signature", ""),
                    }
                )

        if isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                blocks.append(item if isinstance(item, dict) else {"type": "text", "text": str(item)})

        for tool_call in msg.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {})
            args = function.get("arguments", "{}") if isinstance(function, dict) else "{}"
            if isinstance(args, str):
                args = json_repair.loads(args) if args.strip() else {}
            if not isinstance(args, dict):
                args = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_call.get("id") or _gen_tool_id(),
                    "name": function.get("name", "") if isinstance(function, dict) else "",
                    "input": args,
                }
            )

        return blocks or [{"type": "text", "text": ""}]

    @staticmethod
    def _convert_user_content(content: Any) -> Any:
        if isinstance(content, str) or content is None:
            return content or "(empty)"
        if not isinstance(content, list):
            return str(content)

        result: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                result.append({"type": "text", "text": str(item)})
                continue
            if item.get("type") == "image_url":
                converted = AnthropicProvider._convert_image_block(item)
                if converted:
                    result.append(converted)
                continue
            if not item.get("type"):
                result.append({"type": "text", "text": str(item)})
                continue
            result.append(item)
        return result or "(empty)"

    @staticmethod
    def _convert_image_block(block: dict[str, Any]) -> dict[str, Any] | None:
        url = (block.get("image_url") or {}).get("url", "")
        if not url:
            return None
        match = re.match(r"data:(image/\w+);base64,(.+)", url, re.DOTALL)
        if match:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": match.group(1),
                    "data": match.group(2),
                },
            }
        return {"type": "image", "source": {"type": "url", "url": url}}

    @staticmethod
    def _has_tool_use(msg: dict[str, Any]) -> bool:
        content = msg.get("content")
        return isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )

    @staticmethod
    def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                prev_content = merged[-1]["content"]
                current_content = msg["content"]
                if isinstance(prev_content, str):
                    prev_content = [{"type": "text", "text": prev_content}]
                if isinstance(current_content, str):
                    current_content = [{"type": "text", "text": current_content}]
                if isinstance(current_content, list):
                    prev_content.extend(current_content)
                merged[-1]["content"] = prev_content
            else:
                merged.append(msg)

        last_popped: dict[str, Any] | None = None
        while merged and merged[-1].get("role") == "assistant":
            last_popped = merged.pop()

        if not merged and last_popped is not None and not AnthropicProvider._has_tool_use(last_popped):
            merged.append({"role": "user", "content": last_popped.get("content")})

        if merged and merged[0].get("role") == "assistant" and not AnthropicProvider._has_tool_use(merged[0]):
            merged.insert(0, {"role": "user", "content": "(conversation continued)"})

        return merged

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        result: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function", tool)
            if not isinstance(function, dict):
                continue
            entry: dict[str, Any] = {
                "name": function.get("name", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
            description = function.get("description")
            if description:
                entry["description"] = description
            if "cache_control" in tool:
                entry["cache_control"] = tool["cache_control"]
            result.append(entry)
        return result or None

    @staticmethod
    def _convert_tool_choice(
        tool_choice: str | dict[str, Any] | None,
        *,
        thinking_enabled: bool = False,
    ) -> dict[str, Any] | None:
        if thinking_enabled:
            return {"type": "auto"}
        if tool_choice is None or tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return None
        if isinstance(tool_choice, dict):
            name = tool_choice.get("function", {}).get("name")
            if name:
                return {"type": "tool", "name": name}
        return {"type": "auto"}

    @classmethod
    def _apply_cache_control(
        cls,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]] | None]:
        marker = {"type": "ephemeral"}

        if isinstance(system, str) and system:
            system = [{"type": "text", "text": system, "cache_control": marker}]
        elif isinstance(system, list) and system:
            system = list(system)
            system[-1] = {**system[-1], "cache_control": marker}

        new_messages = list(messages)
        if len(new_messages) >= 3:
            msg = new_messages[-2]
            content = msg.get("content")
            if isinstance(content, str):
                new_messages[-2] = {
                    **msg,
                    "content": [{"type": "text", "text": content, "cache_control": marker}],
                }
            elif isinstance(content, list) and content:
                new_content = list(content)
                new_content[-1] = {**new_content[-1], "cache_control": marker}
                new_messages[-2] = {**msg, "content": new_content}

        new_tools = tools
        if tools:
            new_tools = list(tools)
            for index in _tool_cache_marker_indices(new_tools):
                new_tools[index] = {**new_tools[index], "cache_control": marker}

        return system, new_messages, new_tools

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None = None,
        supports_caching: bool = True,
    ) -> dict[str, Any]:
        model_name = self._strip_prefix(model or self.default_model)
        system, anthropic_messages = self._convert_messages(self._sanitize_empty_content(messages))
        anthropic_tools = self._convert_tools(tools)

        if supports_caching:
            system, anthropic_messages, anthropic_tools = self._apply_cache_control(
                system,
                anthropic_messages,
                anthropic_tools,
            )

        max_tokens = max(1, max_tokens)
        thinking_enabled = bool(reasoning_effort) and reasoning_effort.lower() != "none"
        omit_temperature = "opus-4-7" in model_name

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        if system:
            kwargs["system"] = system

        if reasoning_effort == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
            if not omit_temperature:
                kwargs["temperature"] = 1.0
        elif thinking_enabled:
            budget_map = {"low": 1024, "medium": 4096, "high": max(8192, max_tokens)}
            budget = budget_map.get(reasoning_effort.lower(), 4096)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            kwargs["max_tokens"] = max(max_tokens, budget + 4096)
            if not omit_temperature:
                kwargs["temperature"] = 1.0
        elif not omit_temperature:
            kwargs["temperature"] = temperature

        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
            converted_choice = self._convert_tool_choice(
                tool_choice,
                thinking_enabled=thinking_enabled,
            )
            if converted_choice:
                kwargs["tool_choice"] = converted_choice

        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        return kwargs

    @staticmethod
    def _parse_response(response: Any) -> LLMResponse:
        content_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        thinking_blocks: list[dict[str, Any]] = []

        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                content_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCallRequest(
                        id=getattr(block, "id", "") or "",
                        name=getattr(block, "name", "") or "",
                        arguments=(
                            getattr(block, "input", {})
                            if isinstance(getattr(block, "input", {}), dict)
                            else {}
                        ),
                    )
                )
            elif block_type == "thinking":
                thinking_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", "") or "",
                        "signature": getattr(block, "signature", "") or "",
                    }
                )

        stop_map = {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length"}
        finish_reason = stop_map.get(
            getattr(response, "stop_reason", None) or "",
            getattr(response, "stop_reason", None) or "stop",
        )

        usage: dict[str, int] = {}
        raw_usage = getattr(response, "usage", None)
        if raw_usage:
            input_tokens = int(getattr(raw_usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(raw_usage, "output_tokens", 0) or 0)
            cache_creation = int(getattr(raw_usage, "cache_creation_input_tokens", 0) or 0)
            cache_read = int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0)
            prompt_tokens = input_tokens + cache_creation + cache_read
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": prompt_tokens + output_tokens,
            }
            if cache_creation:
                usage["cache_creation_input_tokens"] = cache_creation
            if cache_read:
                usage["cache_read_input_tokens"] = cache_read
                usage["cached_tokens"] = cache_read

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            thinking_blocks=thinking_blocks or None,
        )

    @staticmethod
    def _is_streaming_required_error(exc: Exception) -> bool:
        return isinstance(exc, ValueError) and "streaming is required" in str(exc).lower()

    @classmethod
    def _handle_error(cls, exc: Exception) -> LLMResponse:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        payload = (
            getattr(exc, "body", None)
            or getattr(exc, "doc", None)
            or getattr(response, "text", None)
        )
        if payload is None and response is not None:
            response_json = getattr(response, "json", None)
            if callable(response_json):
                try:
                    payload = response_json()
                except Exception:
                    payload = None

        payload_text = payload if isinstance(payload, str) else str(payload) if payload is not None else ""
        message = (
            f"Error: {payload_text.strip()[:500]}"
            if payload_text.strip()
            else f"Error calling LLM: {exc}"
        )
        retry_after = (
            _retry_after_seconds(headers.get("retry-after"))
            if isinstance(headers, dict)
            else None
        )

        status_code = getattr(exc, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        should_retry: bool | None = None
        if headers is not None:
            raw = headers.get("x-should-retry")
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered == "true":
                    should_retry = True
                elif lowered == "false":
                    should_retry = False

        error_kind: str | None = None
        error_name = exc.__class__.__name__.lower()
        if "timeout" in error_name:
            error_kind = "timeout"
        elif "connection" in error_name:
            error_kind = "connection"
        error_type, error_code = _error_type_code(payload)

        return LLMResponse(
            content=message,
            finish_reason="error",
            error_status_code=int(status_code) if status_code is not None else None,
            error_kind=error_kind,
            error_type=error_type,
            error_code=error_code,
            error_retry_after_s=retry_after,
            error_should_retry=should_retry,
        )

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
        if on_text_delta is not None:
            return await self.chat_stream(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
                on_content_delta=on_text_delta,
            )
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
            response = await self._client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as exc:
            if self._is_streaming_required_error(exc):
                return await self.chat_stream(
                    messages=messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                )
            return self._handle_error(exc)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
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
        idle_timeout_s = int(os.environ.get("YUANCLAW_STREAM_IDLE_TIMEOUT_S", "90"))
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                if on_content_delta or on_thinking_delta or on_tool_call_delta:
                    tool_blocks: dict[int, dict[str, str]] = {}
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=idle_timeout_s,
                            )
                        except StopAsyncIteration:
                            break
                        if getattr(chunk, "type", None) == "content_block_start":
                            block = getattr(chunk, "content_block", None)
                            if getattr(block, "type", None) == "tool_use":
                                index = int(getattr(chunk, "index", 0) or 0)
                                state = {
                                    "call_id": str(getattr(block, "id", "") or ""),
                                    "name": str(getattr(block, "name", "") or ""),
                                }
                                tool_blocks[index] = state
                                if on_tool_call_delta:
                                    await on_tool_call_delta(
                                        {"index": index, **state, "arguments_delta": ""}
                                    )
                        elif (
                            getattr(chunk, "type", None) == "content_block_delta"
                            and getattr(getattr(chunk, "delta", None), "type", None)
                            == "thinking_delta"
                        ):
                            piece = getattr(chunk.delta, "thinking", None) or ""
                            if piece and on_thinking_delta:
                                await on_thinking_delta(piece)
                        elif (
                            getattr(chunk, "type", None) == "content_block_delta"
                            and getattr(getattr(chunk, "delta", None), "type", None)
                            == "text_delta"
                        ):
                            text = getattr(chunk.delta, "text", None) or ""
                            if text and on_content_delta:
                                await on_content_delta(text)
                        elif (
                            getattr(chunk, "type", None) == "content_block_delta"
                            and getattr(getattr(chunk, "delta", None), "type", None)
                            == "input_json_delta"
                        ):
                            partial = getattr(chunk.delta, "partial_json", None) or ""
                            if partial and on_tool_call_delta:
                                index = int(getattr(chunk, "index", 0) or 0)
                                state = tool_blocks.get(index, {})
                                await on_tool_call_delta(
                                    {
                                        "index": index,
                                        "call_id": state.get("call_id", ""),
                                        "name": state.get("name", ""),
                                        "arguments_delta": partial,
                                    }
                                )
                response = await asyncio.wait_for(
                    stream.get_final_message(),
                    timeout=idle_timeout_s,
                )
            return self._parse_response(response)
        except asyncio.TimeoutError:
            return LLMResponse(
                content=(
                    f"Error calling LLM: stream stalled for more than "
                    f"{idle_timeout_s} seconds"
                ),
                finish_reason="error",
                error_kind="timeout",
            )
        except Exception as exc:
            return self._handle_error(exc)

    def get_default_model(self) -> str:
        return self.default_model

    async def aclose(self) -> None:
        await self._close_resource(self._client)
