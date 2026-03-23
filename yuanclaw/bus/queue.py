"""Async message queue for decoupled channel-agent communication."""

import asyncio
from collections.abc import Callable

from yuanclaw.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._inbound_listeners: list[Callable[[InboundMessage], None]] = []
        self._outbound_listeners: list[Callable[[OutboundMessage], None]] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)
        for callback in self._inbound_listeners:
            try:
                callback(msg)
            except Exception:
                # Listener failures must not affect message flow.
                continue

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)
        for callback in self._outbound_listeners:
            try:
                callback(msg)
            except Exception:
                continue

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    def add_inbound_listener(self, callback: Callable[[InboundMessage], None]) -> None:
        """Register an inbound publish listener."""
        self._inbound_listeners.append(callback)

    def add_outbound_listener(self, callback: Callable[[OutboundMessage], None]) -> None:
        """Register an outbound publish listener."""
        self._outbound_listeners.append(callback)

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
