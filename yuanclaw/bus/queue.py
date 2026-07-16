"""Async message queue for decoupled channel-agent communication."""

import asyncio
from collections.abc import Callable

from yuanclaw.bus.events import InboundMessage, OutboundMessage

_CLOSED = object()


class MessageBusClosedError(RuntimeError):
    """Raised when work is published to or consumed from a closed bus."""


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage | object] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage | object] = asyncio.Queue()
        self._inbound_listeners: list[Callable[[InboundMessage], None]] = []
        self._outbound_listeners: list[Callable[[OutboundMessage], None]] = []
        self._closed = False

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        self._ensure_open()
        await self.inbound.put(msg)
        for callback in self._inbound_listeners:
            try:
                callback(msg)
            except Exception:
                # Listener failures must not affect message flow.
                continue

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        item = await self.inbound.get()
        if item is _CLOSED:
            self.inbound.put_nowait(_CLOSED)
            raise MessageBusClosedError("message bus is closed")
        return item  # type: ignore[return-value]

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        self._ensure_open()
        await self.outbound.put(msg)
        for callback in self._outbound_listeners:
            try:
                callback(msg)
            except Exception:
                continue

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        item = await self.outbound.get()
        if item is _CLOSED:
            self.outbound.put_nowait(_CLOSED)
            raise MessageBusClosedError("message bus is closed")
        return item  # type: ignore[return-value]

    def close(self, *, discard_pending: bool = False) -> None:
        """Close the bus, wake blocked consumers, and optionally discard queued messages."""
        if self._closed:
            return
        self._closed = True
        if discard_pending:
            self.drain()
        self.inbound.put_nowait(_CLOSED)
        self.outbound.put_nowait(_CLOSED)

    def drain(self) -> tuple[list[InboundMessage], list[OutboundMessage]]:
        """Remove and return messages currently waiting on both queues."""
        inbound = self._drain_queue(self.inbound)
        outbound = self._drain_queue(self.outbound)
        return inbound, outbound

    @staticmethod
    def _drain_queue(queue: asyncio.Queue) -> list:
        items = []
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not _CLOSED:
                items.append(item)
        return items

    def _ensure_open(self) -> None:
        if self._closed:
            raise MessageBusClosedError("message bus is closed")

    @property
    def closed(self) -> bool:
        return self._closed

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
