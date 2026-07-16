import asyncio
import threading
from time import monotonic

import pytest

from yuanclaw.agent.tools.web import WebSearchTool


@pytest.mark.asyncio
async def test_duckduckgo_cancellation_returns_without_releasing_active_slot(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_search(_self, _query, *, max_results):
        started.set()
        release.wait(timeout=1)
        return []

    monkeypatch.setattr("ddgs.ddgs.DDGS.text", blocking_search)
    tool = WebSearchTool(provider="duckduckgo")
    task = asyncio.create_task(tool.execute("query", count=1))
    try:
        assert await asyncio.to_thread(started.wait, 1)

        before_cancel = monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert monotonic() - before_cancel < 0.5
        assert tool._duckduckgo_slots._value == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    for _ in range(100):
        if tool._duckduckgo_slots._value == 2:
            break
        await asyncio.sleep(0.01)
    assert tool._duckduckgo_slots._value == 2
