from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from yuanclaw.agent.tools import web as web_tools
from yuanclaw.agent.tools.web import WebFetchTool

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"demo-bytes"


class _FakeResponse:
    def __init__(
        self,
        url: str,
        content_type: str,
        body: bytes,
        *,
        status_code: int = 200,
        location: str | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if location is not None:
            self.headers["location"] = location
        self.content = body
        self._body = body

    def raise_for_status(self) -> None:
        return None

    async def aread(self) -> bytes:
        return self._body

    async def aiter_bytes(self):
        midpoint = max(1, len(self._body) // 2)
        yield self._body[:midpoint]
        yield self._body[midpoint:]


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, *args, response: _FakeResponse, **kwargs) -> None:
        self._response = response

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict[str, str]) -> _FakeStream:
        return _FakeStream(self._response)


class _SequenceAsyncClient(_FakeAsyncClient):
    def __init__(self, *args, responses: list[_FakeResponse], **kwargs) -> None:
        self._responses = iter(responses)
        self.requested_urls: list[str] = []

    def stream(self, method: str, url: str, headers: dict[str, str]) -> _FakeStream:
        self.requested_urls.append(url)
        return _FakeStream(next(self._responses))


def test_url_validation_rejects_domain_with_non_public_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda *args: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )

    is_valid, _ = web_tools._validate_url_target("https://example.com/page")

    assert is_valid is False


@pytest.mark.asyncio
async def test_web_fetch_returns_image_content_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeResponse("https://example.com/image.png", "image/png", PNG_BYTES)

    monkeypatch.setattr(web_tools, "_validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(web_tools, "_validate_resolved_url", lambda url: (True, ""))
    monkeypatch.setattr(web_tools.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(*a, response=response, **kw))

    tool = WebFetchTool()
    result = await tool.execute("https://example.com/image.png")

    assert isinstance(result, list)
    assert result[0]["type"] == "image_url"
    assert result[1]["text"] == "(Image fetched from: https://example.com/image.png)"


@pytest.mark.asyncio
async def test_web_fetch_uses_one_direct_fetch_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_tools, "_validate_url_target", lambda url: (True, ""))
    tool = WebFetchTool()
    tool._fetch_readability = AsyncMock(return_value="direct result")
    tool._fetch_image_payload = AsyncMock(side_effect=AssertionError("duplicate image probe"))
    tool._fetch_jina = AsyncMock(side_effect=AssertionError("unexpected intermediary fetch"))

    result = await tool.execute("https://example.com/page")

    assert result == "direct result"
    tool._fetch_readability.assert_awaited_once_with(
        "https://example.com/page",
        "markdown",
        tool.max_chars,
    )


@pytest.mark.asyncio
async def test_web_fetch_blocks_private_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeResponse("http://127.0.0.1/image.png", "image/png", PNG_BYTES)

    monkeypatch.setattr(web_tools, "_validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(
        web_tools,
        "_validate_resolved_url",
        lambda url: (False, "Blocked private redirect") if "127.0.0.1" in url else (True, ""),
    )
    monkeypatch.setattr(web_tools.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(*a, response=response, **kw))

    tool = WebFetchTool()
    result = await tool.execute("https://example.com/image.png")

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["error"].startswith("Redirect blocked")


@pytest.mark.asyncio
async def test_web_fetch_validates_redirect_before_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        "https://example.com/start",
        "text/plain",
        b"",
        status_code=302,
        location="http://127.0.0.1/private",
    )
    client = _SequenceAsyncClient(responses=[response])
    monkeypatch.setattr(web_tools, "_validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(
        web_tools,
        "_validate_resolved_url",
        lambda url: (False, "blocked target") if "127.0.0.1" in url else (True, ""),
    )
    monkeypatch.setattr(web_tools.httpx, "AsyncClient", lambda *a, **kw: client)

    result = await WebFetchTool().execute("https://example.com/start")

    assert json.loads(result)["error"].startswith("Redirect blocked")
    assert client.requested_urls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_web_fetch_rejects_oversized_image_without_full_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        "https://example.com/image.png",
        "image/png",
        PNG_BYTES,
    )
    response.headers["content-length"] = str(web_tools.MAX_FETCH_IMAGE_BYTES + 1)

    monkeypatch.setattr(web_tools, "_validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(web_tools, "_validate_resolved_url", lambda url: (True, ""))
    monkeypatch.setattr(
        web_tools.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(*a, response=response, **kw),
    )

    result = await WebFetchTool().execute("https://example.com/image.png")

    assert isinstance(result, str)
    assert "response exceeds" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_web_fetch_stops_stream_when_document_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        "https://example.com/page",
        "text/plain",
        b"x" * 32,
    )
    monkeypatch.setattr(web_tools, "MAX_FETCH_DOCUMENT_BYTES", 16)
    monkeypatch.setattr(web_tools, "_validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(web_tools, "_validate_resolved_url", lambda url: (True, ""))
    monkeypatch.setattr(
        web_tools.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(*a, response=response, **kw),
    )
    tool = WebFetchTool()
    monkeypatch.setattr(tool, "_fetch_jina", lambda *args: _async_none())

    result = await tool.execute("https://example.com/page")

    assert isinstance(result, str)
    assert "response exceeds 16 bytes" in json.loads(result)["error"]


async def _async_none():
    return None
