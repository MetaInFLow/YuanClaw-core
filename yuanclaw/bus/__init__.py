"""Message bus module for decoupled channel-agent communication."""

from yuanclaw.bus.events import InboundMessage, OutboundMessage
from yuanclaw.bus.queue import MessageBus, MessageBusClosedError

__all__ = ["MessageBus", "MessageBusClosedError", "InboundMessage", "OutboundMessage"]
