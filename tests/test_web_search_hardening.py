from __future__ import annotations

from typing import Any

import pytest

from yuanclaw.agent.tools.web import (
    MAX_SEARCH_OUTPUT_CHARS,
    WebSearchTool,
    _format_results,
)


def test_search_results_include_provider_and_obey_output_limit() -> None:
    output = _format_results(
        "query",
        [
            {
                "title": "T" * 1_000,
                "url": "https://example.test/" + "u" * 3_000,
                "content": "C" * 5_000,
            }
            for _ in range(20)
        ],
        20,
        provider="demo",
    )

    assert output.startswith("Provider: demo\n")
    assert len(output) <= MAX_SEARCH_OUTPUT_CHARS
    assert output.count("C") <= 20_000


@pytest.mark.asyncio
async def test_searxng_sends_configured_key_and_reports_provider(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.test/result",
                        "content": "Summary",
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(
        "yuanclaw.agent.tools.web.httpx.AsyncClient",
        lambda **kwargs: Client(),
    )
    monkeypatch.setattr(
        "yuanclaw.agent.tools.web._validate_url_target",
        lambda _url: (True, ""),
    )
    tool = WebSearchTool(
        provider="searxng",
        base_url="https://search.example.test",
        api_key="configured-key",
    )

    output = await tool.execute("yuanclaw", count=1)

    assert captured["url"] == "https://search.example.test/search"
    assert captured["headers"]["Authorization"] == "Bearer configured-key"
    assert captured["headers"]["X-API-Key"] == "configured-key"
    assert output.startswith("Provider: searxng\n")
