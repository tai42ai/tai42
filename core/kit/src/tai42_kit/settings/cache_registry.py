"""Central registry of cached settings accessors so ``reset_all_settings``
can drop every cached settings singleton in one call (the live-reload soft
restart re-reads env then resets here)."""

from collections.abc import Callable
from functools import lru_cache
from typing import cast

# Keyed by qualified name so a module re-import replaces its registration
# instead of growing the registry or double-running a hook.
_CACHE_CLEARS: dict[str, Callable[[], None]] = {}
_RESET_HOOKS: dict[str, Callable[[], None]] = {}


def _key(fn: Callable) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"


def settings_cache[F: Callable](fn: F) -> F:
    """Cache a zero-arg settings accessor and register it for global reset."""
    cached = lru_cache(maxsize=1)(fn)
    _CACHE_CLEARS[_key(fn)] = cached.cache_clear
    return cast(F, cached)


def register_settings_reset(fn: Callable[[], None]) -> Callable[[], None]:
    """Register a hook that resets settings-derived state on global reset."""
    _RESET_HOOKS[_key(fn)] = fn
    return fn


def reset_all_settings() -> None:
    """Drop every registered settings cache, then run the reset hooks."""
    for clear in list(_CACHE_CLEARS.values()):
        clear()
    for hook in list(_RESET_HOOKS.values()):
        hook()
