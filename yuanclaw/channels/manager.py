"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from loguru import logger

from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.base import BaseChannel
from yuanclaw.channels.registry import discover_all
from yuanclaw.config.schema import Config


@dataclass(frozen=True)
class OutboundFailure:
    """A bounded diagnostic record for an outbound message that was not delivered."""

    channel: str
    chat_id: str
    error_type: str
    attempts: int


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._channel_queues: dict[str, asyncio.Queue] = {}
        self._send_tasks: dict[str, asyncio.Task] = {}
        self._dead_letters: deque[OutboundFailure] = deque(maxlen=100)

        self._init_channels()

    def _channel_section(self, name: str) -> Any | None:
        """Return the config section for a discovered channel."""
        camel_name = name.split("_", 1)[0] + "".join(part.capitalize() for part in name.split("_")[1:])
        channels_cfg = self.config.channels
        if isinstance(channels_cfg, dict):
            return channels_cfg.get(name) or channels_cfg.get(camel_name)

        model_extra = getattr(channels_cfg, "model_extra", None)
        if isinstance(model_extra, dict):
            if name in model_extra:
                return model_extra[name]
            if camel_name in model_extra:
                return model_extra[camel_name]

        return getattr(channels_cfg, name, None)

    @staticmethod
    def _is_enabled(section: Any) -> bool:
        """Check whether a channel config section is enabled."""
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
        return bool(getattr(section, "enabled", False))

    def _build_channel(self, name: str, cls: type[BaseChannel], section: Any) -> BaseChannel:
        """Instantiate a channel, injecting shared dependencies where needed."""
        if name in {"telegram", "feishu"}:
            return cls(section, self.bus, groq_api_key=self.config.providers.groq.api_key)
        return cls(section, self.bus)

    def _init_channels(self) -> None:
        """Initialize channels discovered from built-ins and plugins."""
        for name, cls in discover_all().items():
            section = self._channel_section(name)
            if section is None or not self._is_enabled(section):
                continue

            try:
                channel = self._build_channel(name, cls, section)
                self.channels[name] = channel
                display_name = getattr(channel, "display_name", name.replace("_", " ").title())
                logger.info("{} channel enabled", display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            await asyncio.gather(self._dispatch_task, return_exceptions=True)
            self._dispatch_task = None
        await self._stop_send_workers()

        # Stop all channels
        async def stop_channel(name: str, channel: BaseChannel) -> None:
            try:
                async with asyncio.timeout(self.config.channels.outbound_send_timeout_s):
                    await channel.stop()
                logger.info("Stopped {} channel", name)
            except TimeoutError:
                logger.error("Timed out stopping {} channel", name)
            except Exception as exc:
                logger.error("Error stopping {} ({})", name, type(exc).__name__)

        await asyncio.gather(
            *(stop_channel(name, channel) for name, channel in self.channels.items())
        )

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await self.bus.consume_outbound()

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if (
                        not msg.metadata.get("_tool_hint")
                        and not self.config.channels.send_progress
                    ):
                        continue

                if msg.metadata.get("_streamed"):
                    continue
                if msg.channel not in self.channels:
                    logger.warning("Unknown channel: {}", msg.channel)
                    self._record_failure(msg, "UnknownChannel", attempts=0)
                    continue

                queue = self._ensure_send_worker(msg.channel)
                try:
                    queue.put_nowait(msg)
                except asyncio.QueueFull:
                    logger.error("Outbound queue for {} is full", msg.channel)
                    self._record_failure(msg, "QueueFull", attempts=0)

            except asyncio.CancelledError:
                break
        await self._stop_send_workers()

    def _ensure_send_worker(self, channel_name: str) -> asyncio.Queue:
        queue = self._channel_queues.get(channel_name)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.config.channels.outbound_queue_size)
            self._channel_queues[channel_name] = queue
        task = self._send_tasks.get(channel_name)
        if task is None or task.done():
            task = asyncio.create_task(
                self._send_worker(channel_name, queue),
                name=f"yuanclaw-outbound-{channel_name}",
            )
            self._send_tasks[channel_name] = task
        return queue

    async def _send_worker(self, channel_name: str, queue: asyncio.Queue) -> None:
        channel = self.channels[channel_name]
        while True:
            msg = await queue.get()
            try:
                await self._deliver_with_retry(channel, msg)
            finally:
                queue.task_done()

    async def _deliver_with_retry(self, channel: BaseChannel, msg) -> None:
        is_stream_marker = bool(
            msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end")
        )
        max_attempts = 1 if is_stream_marker else 1 + self.config.channels.outbound_retry_attempts
        last_error_type = "DeliveryError"
        for attempt in range(1, max_attempts + 1):
            try:
                async with asyncio.timeout(self.config.channels.outbound_send_timeout_s):
                    if is_stream_marker:
                        await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
                    else:
                        await channel.send(msg)
                return
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                last_error_type = "TimeoutError"
            except Exception as exc:
                last_error_type = type(exc).__name__

            if attempt < max_attempts:
                await asyncio.sleep(self.config.channels.outbound_retry_delay_s)

        logger.error(
            "Outbound delivery to {} failed after {} attempt(s) ({})",
            msg.channel,
            max_attempts,
            last_error_type,
        )
        self._record_failure(msg, last_error_type, attempts=max_attempts)

    def _record_failure(self, msg, error_type: str, *, attempts: int) -> None:
        self._dead_letters.append(
            OutboundFailure(
                channel=msg.channel,
                chat_id=msg.chat_id,
                error_type=error_type,
                attempts=attempts,
            )
        )

    async def _stop_send_workers(self) -> None:
        tasks = [task for task in self._send_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._send_tasks.clear()
        self._channel_queues.clear()

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running,
                "pending_outbound": self._channel_queues.get(name).qsize()
                if name in self._channel_queues
                else 0,
            }
            for name, channel in self.channels.items()
        }

    @property
    def dead_letters(self) -> tuple[OutboundFailure, ...]:
        return tuple(self._dead_letters)

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
