"""Auto-discovery for built-in channel modules and external plugins."""

from __future__ import annotations

import importlib
import pkgutil
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from yuanclaw.channels.base import BaseChannel

_INTERNAL = frozenset({"base", "manager", "registry"})
_ENTRY_POINT_GROUPS = ("yuanclaw.channels", "nanobot.channels")


def discover_channel_names() -> list[str]:
    """Return all built-in channel module names by scanning the package."""
    package_dir = Path(__file__).resolve().parent
    return [
        name
        for _, name, ispkg in pkgutil.iter_modules([str(package_dir)])
        if name not in _INTERNAL and not ispkg
    ]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Import *module_name* and return the first BaseChannel subclass found."""
    from yuanclaw.channels.base import BaseChannel as _Base

    mod = importlib.import_module(f"yuanclaw.channels.{module_name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(f"No BaseChannel subclass in yuanclaw.channels.{module_name}")


def discover_plugins() -> dict[str, type[BaseChannel]]:
    """Discover external channel plugins registered via entry points."""
    plugins: dict[str, type[BaseChannel]] = {}
    eps = entry_points()

    for group in _ENTRY_POINT_GROUPS:
        if hasattr(eps, "select"):
            group_eps = eps.select(group=group)
        else:  # pragma: no cover - older importlib.metadata API
            group_eps = eps.get(group, [])

        for ep in group_eps:
            try:
                cls = ep.load()
                plugins[ep.name] = cls
            except Exception as e:
                logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)

    return plugins


def discover_all() -> dict[str, type[BaseChannel]]:
    """Return all channels: built-in modules merged with external plugins."""
    builtin: dict[str, type[BaseChannel]] = {}
    for module_name in discover_channel_names():
        try:
            builtin[module_name] = load_channel_class(module_name)
        except ImportError as e:
            logger.debug("Skipping built-in channel '{}': {}", module_name, e)

    external = discover_plugins()
    shadowed = set(external) & set(builtin)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)

    # Built-ins win so core channels keep their expected implementations.
    return {**external, **builtin}
