from types import SimpleNamespace

import pytest

from yuanclaw.bus.events import OutboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.base import BaseChannel
from yuanclaw.channels.whatsapp import WhatsAppChannel
from yuanclaw.config.schema import WhatsAppConfig


class _DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        return None


def test_is_allowed_requires_exact_match() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_from=["allow@email.com"]), MessageBus())

    assert channel.is_allowed("allow@email.com") is True
    assert channel.is_allowed("attacker|allow@email.com") is False


@pytest.mark.asyncio
async def test_whatsapp_send_reports_disconnected_bridge() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())

    with pytest.raises(ConnectionError, match="not connected"):
        await channel.send(OutboundMessage(channel="whatsapp", chat_id="1", content="hello"))


@pytest.mark.asyncio
async def test_whatsapp_send_failure_marks_connection_unhealthy() -> None:
    class BrokenSocket:
        async def send(self, _payload: str) -> None:
            raise RuntimeError("closed")

    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel._ws = BrokenSocket()
    channel._connected = True

    with pytest.raises(RuntimeError, match="closed"):
        await channel.send(OutboundMessage(channel="whatsapp", chat_id="1", content="hello"))

    assert channel._connected is False
