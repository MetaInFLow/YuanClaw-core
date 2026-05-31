"""Provider wrapper that fails over to fallback models on retryable errors."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from yuanclaw.providers.base import LLMProvider, LLMResponse

_PRIMARY_FAILURE_THRESHOLD = 3
_PRIMARY_COOLDOWN_S = 60
_MISSING = object()
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

    @property
    def generation(self):
        return self._primary.generation

    @generation.setter
    def generation(self, value):
        self._primary.generation = value

    def get_default_model(self) -> str:
        return self._primary.get_default_model()

    def _primary_available(self) -> bool:
        if self._primary_tripped_at is None:
            return True
        if time.monotonic() - self._primary_tripped_at >= _PRIMARY_COOLDOWN_S:
            return True
        return False

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

        if self._primary_available():
            response = await call(self._primary, kwargs)
            if response.finish_reason != "error":
                self._primary_failures = 0
                self._primary_tripped_at = None
                return response

            if not self._should_fallback(response):
                logger.warning(
                    "Primary model '{}' returned non-fallbackable error: {}",
                    primary_model,
                    (response.content or "")[:120],
                )
                return response

            self._primary_failures += 1
            if self._primary_failures >= _PRIMARY_FAILURE_THRESHOLD:
                self._primary_tripped_at = time.monotonic()
                logger.warning(
                    "Primary model '{}' circuit open after {} consecutive failures",
                    primary_model,
                    self._primary_failures,
                )
        else:
            logger.debug("Primary model '{}' circuit open; skipping", primary_model)

        last_response: LLMResponse | None = None
        primary_skipped = not self._primary_available()
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

            original_values = {
                name: kwargs.get(name, _MISSING)
                for name in ("model", "max_tokens", "temperature", "reasoning_effort")
            }
            kwargs["model"] = fallback_model
            kwargs["max_tokens"] = fallback.max_tokens
            kwargs["temperature"] = fallback.temperature
            if fallback.reasoning_effort is None:
                kwargs.pop("reasoning_effort", None)
            else:
                kwargs["reasoning_effort"] = fallback.reasoning_effort
            try:
                fallback_response = await call(fallback_provider, kwargs)
            finally:
                for name, value in original_values.items():
                    if value is _MISSING:
                        kwargs.pop(name, None)
                    else:
                        kwargs[name] = value

            if fallback_response.finish_reason != "error":
                logger.info(
                    "Fallback '{}' succeeded after primary '{}' failed",
                    fallback_model,
                    primary_model,
                )
                return fallback_response

            last_response = fallback_response
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
