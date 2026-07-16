import asyncio
from contextlib import suppress

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.web import WebSearchTool
from yuanclaw.bus.events import OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.base import BaseChannel
from yuanclaw.channels.manager import ChannelManager
from yuanclaw.config.schema import Config
from yuanclaw.providers.base import LLMResponse


class _FakePluginChannel(BaseChannel):
    name = "demo_plugin"
    display_name = "Demo Plugin"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg) -> None:
        return None


class _FakeStreamChannel(BaseChannel):
    name = "stream"
    display_name = "Stream"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.sent: list[str] = []
        self.deltas: list[tuple[str, str, dict]] = []
        self.finished = asyncio.Event()
        self.sent_event = asyncio.Event()

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg) -> None:
        self.sent.append(msg.content)
        self.sent_event.set()

    async def send_delta(self, chat_id: str, delta: str, metadata=None) -> None:
        self.deltas.append((chat_id, delta, dict(metadata or {})))
        if metadata and metadata.get("_stream_end") and not metadata.get("_resuming"):
            self.finished.set()


class _FakeStreamProvider:
    def get_default_model(self) -> str:
        return "demo"

    async def chat_stream(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        on_content_delta=None,
    ) -> LLMResponse:
        if on_content_delta:
            await on_content_delta("Hel")
            await on_content_delta("lo")
        return LLMResponse(content="Hello", finish_reason="stop")


class _BlockingChannel(_FakeStreamChannel):
    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, msg) -> None:
        self.started.set()
        await self.release.wait()
        await super().send(msg)


class _FailingChannel(_FakeStreamChannel):
    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.attempts = 0

    async def send(self, msg) -> None:
        self.attempts += 1
        raise RuntimeError("send failed")


def test_channels_config_preserves_plugin_sections(monkeypatch):
    monkeypatch.setattr(
        "yuanclaw.channels.manager.discover_all",
        lambda: {"demo_plugin": _FakePluginChannel},
    )

    config = Config.model_validate(
        {
            "channels": {
                "sendProgress": True,
                "demoPlugin": {
                    "enabled": True,
                    "allowFrom": ["*"],
                },
            }
        }
    )

    manager = ChannelManager(config, MessageBus())
    assert "demo_plugin" in manager.channels


def test_channel_sections_preserve_extra_runtime_flags():
    config = Config.model_validate(
        {
            "channels": {
                "telegram": {
                    "enabled": True,
                    "allowFrom": ["*"],
                    "streaming": True,
                }
            }
        }
    )

    assert getattr(config.channels.telegram, "streaming", False) is True


def test_local_provider_fallback_routes_plain_model_to_ollama():
    config = Config()
    config.providers.ollama.api_base = "http://localhost:11434"

    assert config.get_provider_name("llama3.2") == "ollama"
    assert config.get_api_base("llama3.2") == "http://localhost:11434"


def test_web_search_unknown_provider_returns_error():
    tool = WebSearchTool(provider="unknown")
    result = asyncio.run(tool.execute("yuanclaw"))

    assert "unknown search provider" in result


def test_web_search_duckduckgo_dispatch(monkeypatch):
    tool = WebSearchTool(provider="duckduckgo")

    async def _fake_ddg(query: str, n: int) -> str:
        return f"{query}:{n}"

    monkeypatch.setattr(tool, "_search_duckduckgo", _fake_ddg)
    result = asyncio.run(tool.execute("yuanclaw", count=3))

    assert result == "yuanclaw:3"


@pytest.mark.asyncio
async def test_agent_loop_uses_chat_stream_when_available(tmp_path):
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeStreamProvider(),
        workspace=tmp_path,
    )
    deltas: list[str] = []
    ended: list[bool] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    async def on_end(*, resuming: bool = False) -> None:
        ended.append(resuming)

    response = await loop.process_direct(
        "hello",
        on_stream=on_delta,
        on_stream_end=on_end,
    )

    assert response == "Hello"
    assert deltas == ["Hel", "lo"]
    assert ended == [False]


@pytest.mark.asyncio
async def test_channel_manager_routes_stream_markers_to_send_delta():
    manager = ChannelManager(Config(), MessageBus())
    channel = _FakeStreamChannel({"enabled": True, "allow_from": ["*"], "streaming": True}, manager.bus)
    manager.channels = {"stream": channel}

    task = asyncio.create_task(manager._dispatch_outbound())
    try:
        await manager.bus.publish_outbound(
            OutboundMessage(
                channel="stream",
                chat_id="123",
                content="Hel",
                metadata={"_stream_delta": True},
            )
        )
        await manager.bus.publish_outbound(
            OutboundMessage(
                channel="stream",
                chat_id="123",
                content="",
                metadata={"_stream_end": True, "_resuming": False},
            )
        )
        await manager.bus.publish_outbound(
            OutboundMessage(
                channel="stream",
                chat_id="123",
                content="ignored",
                metadata={"_streamed": True},
            )
        )

        await asyncio.wait_for(channel.finished.wait(), timeout=1.0)
        assert channel.deltas == [
            ("123", "Hel", {"_stream_delta": True}),
            ("123", "", {"_stream_end": True, "_resuming": False}),
        ]
        assert channel.sent == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_channel_manager_isolates_slow_channel_delivery():
    manager = ChannelManager(Config(), MessageBus())
    slow = _BlockingChannel({"enabled": True, "allow_from": ["*"]}, manager.bus)
    fast = _FakeStreamChannel({"enabled": True, "allow_from": ["*"]}, manager.bus)
    manager.channels = {"slow": slow, "fast": fast}
    task = asyncio.create_task(manager._dispatch_outbound())
    try:
        await manager.bus.publish_outbound(
            OutboundMessage(channel="slow", chat_id="1", content="slow")
        )
        await asyncio.wait_for(slow.started.wait(), timeout=1.0)
        await manager.bus.publish_outbound(
            OutboundMessage(channel="fast", chat_id="2", content="fast")
        )

        await asyncio.wait_for(fast.sent_event.wait(), timeout=0.2)
        assert fast.sent == ["fast"]
        assert slow.sent == []
    finally:
        slow.release.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_channel_manager_retries_then_records_dead_letter():
    config = Config()
    config.channels.outbound_retry_attempts = 1
    config.channels.outbound_retry_delay_s = 0
    manager = ChannelManager(config, MessageBus())
    failing = _FailingChannel({"enabled": True, "allow_from": ["*"]}, manager.bus)
    manager.channels = {"failing": failing}
    task = asyncio.create_task(manager._dispatch_outbound())
    try:
        await manager.bus.publish_outbound(
            OutboundMessage(channel="failing", chat_id="3", content="hello")
        )
        for _ in range(100):
            if manager.dead_letters:
                break
            await asyncio.sleep(0.01)

        assert failing.attempts == 2
        assert len(manager.dead_letters) == 1
        failure = manager.dead_letters[0]
        assert failure.channel == "failing"
        assert failure.chat_id == "3"
        assert failure.error_type == "RuntimeError"
        assert failure.attempts == 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
