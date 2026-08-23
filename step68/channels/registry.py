from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

import step68.channels as channels_pkg

if TYPE_CHECKING:
    from step68.channel import BaseChannel

_INTERNAL = frozenset({"base", "manager", "registry"})
DEFAULT_ENABLED_CHANNELS = frozenset({"cli"})


def discover_channel_names() -> list[str]:
    """Return all built-in channel module names by scanning the package (zero imports)."""
    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(channels_pkg.__path__)
        if name not in _INTERNAL and not name.startswith("_") and not ispkg
    ]


def load_channel_class(module_name: str) -> type["BaseChannel"]:
    """Import *module_name* and return the first BaseChannel subclass found."""
    from step68.channel import BaseChannel

    mod = importlib.import_module(f"step64.channels.{module_name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, BaseChannel) and obj is not BaseChannel:
            return obj
    raise ImportError(f"No BaseChannel subclass in step64.channels.{module_name}")
