"""Settings cache-accessor protocol.

The concrete settings primitives (``TaiBaseSettings``, the ``settings_cache`` /
``register_settings_reset`` / ``reset_all_settings`` registry) are leaf code and
live in the implementing layer. This protocol names the seam the platform depends on: a
registry that can drop every cached settings singleton in one call (the live
reload soft-restart re-reads env then resets here).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SettingsCacheRegistry(Protocol):
    """The settings-cache reset seam."""

    def settings_cache[F: Callable[..., Any]](self, fn: F) -> F:
        """Cache a zero-arg settings accessor and register it for global reset."""
        ...

    def register_settings_reset(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Register a hook that resets settings-derived state on global reset."""
        ...

    def reset_all_settings(self) -> None:
        """Drop every registered settings cache, then run the reset hooks."""
        ...


__all__ = ["SettingsCacheRegistry"]
