import json

import pytest

from yuanclaw.providers.openai_codex_provider import _consume_sse, _extract_usage


class _FakeResponse:
    def __init__(self, events):
        self._events = events

    async def aiter_lines(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}"
            yield ""


def test_extract_usage_accepts_responses_api_shape() -> None:
    usage = _extract_usage(
        {
            "usage": {
                "input_tokens": 12,
                "output_tokens": 8,
                "total_tokens": 20,
            }
        }
    )

    assert usage == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }


@pytest.mark.asyncio
async def test_consume_sse_reads_usage_from_response_completed() -> None:
    response = _FakeResponse(
        [
            {"type": "response.output_text.delta", "delta": "hello"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 21,
                        "output_tokens": 9,
                        "total_tokens": 30,
                    },
                },
            },
        ]
    )

    content, tool_calls, finish_reason, usage = await _consume_sse(response)

    assert content == "hello"
    assert tool_calls == []
    assert finish_reason == "stop"
    assert usage == {
        "prompt_tokens": 21,
        "completion_tokens": 9,
        "total_tokens": 30,
    }
