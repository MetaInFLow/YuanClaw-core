import asyncio

import pytest

from yuanclaw.bus.events import InboundMessage, OutboundMessage
from yuanclaw.bus.queue import MessageBus, MessageBusClosedError


@pytest.mark.asyncio
async def test_close_wakes_blocked_consumers_and_rejects_new_messages() -> None:
    bus = MessageBus()
    inbound_waiter = asyncio.create_task(bus.consume_inbound())
    outbound_waiter = asyncio.create_task(bus.consume_outbound())
    await asyncio.sleep(0)

    bus.close()

    with pytest.raises(MessageBusClosedError):
        await inbound_waiter
    with pytest.raises(MessageBusClosedError):
        await outbound_waiter
    with pytest.raises(MessageBusClosedError):
        await bus.publish_inbound(_inbound("late"))
    with pytest.raises(MessageBusClosedError):
        await bus.publish_outbound(_outbound("late"))
    assert bus.closed is True


@pytest.mark.asyncio
async def test_drain_returns_pending_messages_before_shutdown() -> None:
    bus = MessageBus()
    inbound = _inbound("request")
    outbound = _outbound("response")
    await bus.publish_inbound(inbound)
    await bus.publish_outbound(outbound)

    pending_inbound, pending_outbound = bus.drain()

    assert pending_inbound == [inbound]
    assert pending_outbound == [outbound]
    assert bus.inbound_size == 0
    assert bus.outbound_size == 0


def _inbound(content: str) -> InboundMessage:
    return InboundMessage(
        channel="test",
        sender_id="user",
        chat_id="chat",
        content=content,
    )


def _outbound(content: str) -> OutboundMessage:
    return OutboundMessage(channel="test", chat_id="chat", content=content)
