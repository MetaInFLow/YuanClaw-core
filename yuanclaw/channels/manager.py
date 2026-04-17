"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.base import BaseChannel
from yuanclaw.channels.registry import discover_all
from yuanclaw.config.schema import Config


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
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(self.bus.consume_outbound(), timeout=1.0)

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if (
                        not msg.metadata.get("_tool_hint")
                        and not self.config.channels.send_progress
                    ):
                        continue

                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        if msg.metadata.get("_stream_delta"):
                            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
                        elif msg.metadata.get("_stream_end"):
                            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
                        elif msg.metadata.get("_streamed"):
                            continue
                        else:
                            await channel.send(msg)
                    except Exception as e:
                        logger.error("Error sending to {}: {}", msg.channel, e)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {"enabled": True, "running": channel.is_running}
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
