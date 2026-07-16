"""Provider wrapper that fails over to fallback models on retryable errors."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from yuanclaw.providers.base import LLMProvider, LLMResponse

_PRIMARY_FAILURE_THRESHOLD = 3
_PRIMARY_COOLDOWN_S = 60
_FALLBACK_ERROR_KINDS = frozenset({
    "timeout",
    "connection",
    "server_error",
    "rate_limit",
    "overloaded",
})
_NON_FALLBACK_ERROR_KINDS = frozenset({
    "authentication",
    "auth",
    "permission",
    "content_filter",
    "refusal",
    "context_length",
    "invalid_request",
})
_FALLBACK_ERROR_TOKENS = (
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "too many requests",
    "overloaded",
    "server_error",
    "server error",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "connection",
    "insufficient_quota",
    "insufficient quota",
    "quota_exceeded",
    "quota exceeded",
    "quota_exhausted",
    "quota exhausted",
    "billing_hard_limit",
    "insufficient_balance",
    "balance",
    "out of credits",
)


class FallbackProvider(LLMProvider):
    """Wrap a primary provider and transparently fail over to configured fallbacks."""

    def __init__(
        self,
        primary: LLMProvider,
        fallback_presets: list[Any],
        provider_factory: Callable[[Any], LLMProvider],
    ) -> None:
        self._primary = primary
        self._fallback_presets = list(fallback_presets)
        self._provider_factory = provider_factory
        self._primary_failures = 0
        self._primary_tripped_at: float | None = None
        self._circuit_lock = asyncio.Lock()
        self._primary_attempt_seq = 0
        self._primary_applied_seq = 0
        self._primary_outcomes: dict[int, bool | None] = {}

    @property
    def generation(self):
        return self._primary.generation

    @generation.setter
    def generation(self, value):
        self._primary.generation = value

    def get_default_model(self) -> str:
        return self._primary.get_default_model()

    def _primary_available_unlocked(self) -> bool:
        if self._primary_tripped_at is None:
            return True
        if time.monotonic() - self._primary_tripped_at >= _PRIMARY_COOLDOWN_S:
            return True
        return False

    async def _begin_primary_attempt(self) -> int | None:
        async with self._circuit_lock:
            if not self._primary_available_unlocked():
                return None
            self._primary_attempt_seq += 1
            return self._primary_attempt_seq

    async def _record_primary_outcome(
        self,
        attempt_id: int,
        succeeded: bool | None,
    ) -> None:
        async with self._circuit_lock:
            self._primary_outcomes[attempt_id] = succeeded
            while self._primary_applied_seq + 1 in self._primary_outcomes:
                self._primary_applied_seq += 1
                outcome = self._primary_outcomes.pop(self._primary_applied_seq)
                if outcome is True:
                    self._primary_failures = 0
                    self._primary_tripped_at = None
                elif outcome is False:
                    self._primary_failures += 1
                    if self._primary_failures >= _PRIMARY_FAILURE_THRESHOLD:
                        self._primary_tripped_at = time.monotonic()

    async def _primary_is_tripped(self) -> bool:
        async with self._circuit_lock:
            return not self._primary_available_unlocked()

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if not self._fallback_presets:
            return await self._primary.chat(**kwargs)
        return await self._try_with_fallback(lambda p, kw: p.chat(**kw), kwargs)

    async def _try_with_fallback(
        self,
        call: Callable[[LLMProvider, dict[str, Any]], Awaitable[LLMResponse]],
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        primary_model = kwargs.get("model") or self._primary.get_default_model()
        last_response: LLMResponse | None = None

        primary_attempt_id = await self._begin_primary_attempt()
        if primary_attempt_id is not None:
            try:
                response, emitted = await self._attempt(call, self._primary, kwargs)
            except asyncio.CancelledError:
                await self._record_primary_outcome(primary_attempt_id, None)
                raise
            last_response = response
            if not self._is_error_response(response):
                await self._record_primary_outcome(primary_attempt_id, True)
                return response

            if emitted:
                logger.warning(
                    "Primary model '{}' failed after streaming output; fallback suppressed",
                    primary_model,
                )
                await self._record_primary_outcome(primary_attempt_id, None)
                return response

            if not self._should_fallback(response):
                logger.warning(
                    "Primary model '{}' returned non-fallbackable error: {}",
                    primary_model,
                    (response.content or "")[:120],
                )
                await self._record_primary_outcome(primary_attempt_id, None)
                return response

            await self._record_primary_outcome(primary_attempt_id, False)
            if await self._primary_is_tripped():
                logger.warning(
                    "Primary model '{}' circuit open after {} consecutive failures",
                    primary_model,
                    self._primary_failures,
                )
        else:
            logger.debug("Primary model '{}' circuit open; skipping", primary_model)

        primary_skipped = await self._primary_is_tripped()
        for idx, fallback in enumerate(self._fallback_presets):
            fallback_model = fallback.model
            if idx == 0 and primary_skipped:
                logger.info(
                    "Primary model '{}' circuit open, trying fallback '{}'",
                    primary_model,
                    fallback_model,
                )
            elif idx == 0:
                logger.info(
                    "Primary model '{}' failed, trying fallback '{}'",
                    primary_model,
                    fallback_model,
                )
            else:
                logger.info(
                    "Fallback '{}' also failed, trying next fallback '{}'",
                    self._fallback_presets[idx - 1].model,
                    fallback_model,
                )

            try:
                fallback_provider = self._provider_factory(fallback)
            except Exception as exc:
                logger.warning("Failed to create provider for fallback '{}': {}", fallback_model, exc)
                continue

            fallback_kwargs = dict(kwargs)
            fallback_kwargs["model"] = fallback_model
            fallback_kwargs["max_tokens"] = fallback.max_tokens
            fallback_kwargs["temperature"] = fallback.temperature
            if fallback.reasoning_effort is None:
                fallback_kwargs.pop("reasoning_effort", None)
            else:
                fallback_kwargs["reasoning_effort"] = fallback.reasoning_effort
            try:
                fallback_response, emitted = await self._attempt(
                    call,
                    fallback_provider,
                    fallback_kwargs,
                )
            finally:
                try:
                    await fallback_provider.aclose()
                except Exception as exc:
                    logger.warning(
                        "Failed to close fallback provider '{}' ({})",
                        fallback_model,
                        type(exc).__name__,
                    )

            if not self._is_error_response(fallback_response):
                logger.info(
                    "Fallback '{}' succeeded after primary '{}' failed",
                    fallback_model,
                    primary_model,
                )
                return fallback_response

            last_response = fallback_response
            if emitted:
                logger.warning(
                    "Fallback '{}' failed after streaming output; further fallback suppressed",
                    fallback_model,
                )
                return fallback_response
            logger.warning(
                "Fallback '{}' also failed: {}",
                fallback_model,
                (fallback_response.content or "")[:120],
            )

        logger.warning("All {} fallback model(s) failed", len(self._fallback_presets))
        return last_response or LLMResponse(
            content=f"Primary model '{primary_model}' circuit open and no fallbacks available",
            finish_reason="error",
        )

    @staticmethod
    async def _attempt(
        call: Callable[[LLMProvider, dict[str, Any]], Awaitable[LLMResponse]],
        provider: LLMProvider,
        kwargs: dict[str, Any],
    ) -> tuple[LLMResponse, bool]:
        emitted = False
        attempt_kwargs = dict(kwargs)
        on_text_delta = attempt_kwargs.get("on_text_delta")
        if on_text_delta is not None:
            async def tracked_delta(delta: str) -> None:
                nonlocal emitted
                emitted = True
                await on_text_delta(delta)

            attempt_kwargs["on_text_delta"] = tracked_delta
        try:
            return await call(provider, attempt_kwargs), emitted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return FallbackProvider._response_from_exception(exc), emitted

    @staticmethod
    def _response_from_exception(exc: Exception) -> LLMResponse:
        name = type(exc).__name__
        lowered = name.lower()
        status = getattr(exc, "status_code", None)
        kind: str | None = None
        should_retry: bool | None = None
        if "timeout" in lowered:
            kind, should_retry = "timeout", True
        elif "connection" in lowered:
            kind, should_retry = "connection", True
        elif "ratelimit" in lowered or status == 429:
            kind, should_retry = "rate_limit", True
        elif "authentication" in lowered or status == 401:
            kind, should_retry = "authentication", False
        elif "permission" in lowered or status == 403:
            kind, should_retry = "permission", False
        elif isinstance(status, int) and status >= 500:
            kind, should_retry = "server_error", True
        return LLMResponse(
            content=f"Provider request failed ({name})",
            finish_reason="error",
            error_status_code=status if isinstance(status, int) else None,
            error_kind=kind,
            error_type=name,
            error_should_retry=should_retry,
        )

    @staticmethod
    def _is_error_response(response: LLMResponse) -> bool:
        return (
            response.finish_reason == "error"
            or response.error_kind is not None
            or response.error_status_code is not None
            or response.error_type is not None
            or response.error_code is not None
        )

    async def aclose(self) -> None:
        """Close the primary provider owned by this wrapper."""
        await self._primary.aclose()

    @staticmethod
    def _should_fallback(response: LLMResponse) -> bool:
        if response.error_should_retry is False:
            return False
        status = response.error_status_code
        kind = (response.error_kind or "").lower()
        error_type = (response.error_type or "").lower()
        code = (response.error_code or "").lower()
        text = (response.content or "").lower()

        if kind in _NON_FALLBACK_ERROR_KINDS or error_type in _NON_FALLBACK_ERROR_KINDS:
            return False
        if kind in _FALLBACK_ERROR_KINDS or error_type in _FALLBACK_ERROR_KINDS:
            return True
        if status is not None and (status == 408 or status == 409 or status == 429 or status >= 500):
            return True
        if any(token in code or token in text for token in _FALLBACK_ERROR_TOKENS):
            return True
        return response.error_should_retry is True
