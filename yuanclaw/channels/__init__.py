"""Chat channels module with plugin architecture."""

from yuanclaw.channels.base import BaseChannel
from yuanclaw.channels.manager import ChannelManager
from yuanclaw.channels.registry import (
    discover_all,
    discover_channel_names,
    discover_plugins,
    load_channel_class,
)
from yuanclaw.channels.wecom import WeComChannel

__all__ = [
    "BaseChannel",
    "ChannelManager",
    "WeComChannel",
    "discover_all",
    "discover_channel_names",
    "discover_plugins",
    "load_channel_class",
]
