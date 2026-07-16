from unittest.mock import AsyncMock

import pytest

from yuanclaw.providers.base import LLMResponse
from yuanclaw.providers.custom_provider import CustomProvider


@pytest.mark.asyncio
async def test_custom_chat_uses_streaming_path_when_delta_callback_is_present() -> None:
    provider = CustomProvider()
    expected = LLMResponse(content="streamed")
    provider.chat_stream = AsyncMock(return_value=expected)

    result = await provider.chat(
        [{"role": "user", "content": "hello"}],
        on_text_delta=AsyncMock(),
    )

    assert result is expected
    provider.chat_stream.assert_awaited_once()
    await provider.aclose()


@pytest.mark.asyncio
async def test_custom_error_response_contains_retry_metadata() -> None:
    provider = CustomProvider()
    error = RuntimeError("service unavailable")
    error.status_code = 503
    error.code = "upstream_unavailable"

    response = provider._handle_error(error)

    assert response.finish_reason == "error"
    assert response.error_status_code == 503
    assert response.error_kind == "server_error"
    assert response.error_code == "upstream_unavailable"
    assert response.error_should_retry is True
    await provider.aclose()
