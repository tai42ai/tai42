"""Central registry of cached settings accessors so ``reset_all_settings``
can drop every cached settings singleton in one call (the live-reload soft
restart re-reads env then resets here).

Each constructed ``BaseSettings`` instance is stamped with the epoch it was born
under and recorded in a weakref roster, so ``sweep_stale_settings`` can find any
instance of a retired epoch that a holder is still keeping alive past a reset — a
stale-config leak, reported loudly and never dropped."""

import contextlib
import gc
import logging
import threading
import types
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Keyed by qualified name so a module re-import replaces its registration
# instead of growing the registry or double-running a hook.
_CACHE_CLEARS: dict[str, Callable[[], None]] = {}
_RESET_HOOKS: dict[str, Callable[[], None]] = {}

# Epoch stamp written on each constructed settings instance via
# ``object.__setattr__`` — invisible to ``model_dump`` (which iterates
# ``model_fields`` only) and weakref-safe. The roster is a list of weakrefs
# (settings instances are unhashable, so no set/dict) pruned by callback as
# instances die, leaving only the still-live ones for the sweep to inspect.
# Every append/remove/snapshot of the roster is serialized under ``_roster_lock``,
# reached concurrently as settings are constructed and swept across threads.
_EPOCH_STAMP_ATTR = "__tai_settings_epoch__"
_stamp_roster: "list[weakref.ref[BaseSettings]]" = []
# An RLock, not a plain Lock: the ``_prune_roster`` weakref callback can fire
# during the ``append`` in ``_stamp_settings`` (a gc triggered on the same thread
# while the lock is held), which a non-reentrant Lock would deadlock.
_roster_lock = threading.RLock()


def _key(fn: Callable) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"


def _current_epoch() -> int:
    # The single process-wide epoch lives in ``clients.base``; import lazily so
    # this module (imported while ``settings`` initialises) takes no import-time
    # dependency on the clients package.
    from tai42_kit.clients.base import current_client_epoch

    return current_client_epoch()


def _prune_roster(dead: "weakref.ref") -> None:
    with _roster_lock, contextlib.suppress(ValueError):
        _stamp_roster.remove(dead)


def _stamp_settings(value: object) -> None:
    """Stamp a constructed settings instance with the current epoch and roster it.

    Only ``BaseSettings`` instances are stamped. The non-model accessors return
    primitives (``str``/``float``/``None`` — e.g. skeleton ``config_mode() ->
    str``) that support neither ``object.__setattr__`` nor ``weakref``, and a
    captured primitive is an immutable stale VALUE the sweep could never reach; so
    those are skipped. They are few, core-owned, and recycle/excluded-class, so
    the miss is acceptable.
    """
    if not isinstance(value, BaseSettings):
        return
    object.__setattr__(value, _EPOCH_STAMP_ATTR, _current_epoch())
    with _roster_lock:
        _stamp_roster.append(weakref.ref(value, _prune_roster))


def settings_cache[F: Callable](fn: F) -> F:
    """Cache a zero-arg settings accessor, register it for reset, and epoch-stamp it.

    Stamping happens on the CONSTRUCTION path (cache miss), so the stamp records
    the epoch the instance was born under; a cached hit returns the same stamped
    instance, and a reset clears the cache so the next call rebuilds and re-stamps
    under the then-current epoch.
    """

    def _construct():
        value = fn()
        _stamp_settings(value)
        return value

    cached = lru_cache(maxsize=1)(_construct)
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


@dataclass(frozen=True)
class StaleHolder:
    """A still-live cached settings instance stamped with a retired epoch.

    ``settings_type`` and ``holders`` are ``module.qualname`` strings (the settings
    class, and the type of each object still referencing the instance)."""

    settings_type: str
    epoch: int
    holders: tuple[str, ...]


def _summarize_holders(instance: object) -> tuple[str, ...]:
    # Referrers minus this sweep's own transient scaffolding: the frame walking
    # the roster and the referrers list itself would otherwise show up as holders.
    referrers = gc.get_referrers(instance)
    return tuple(
        f"{type(r).__module__}.{type(r).__qualname__}"
        for r in referrers
        if not isinstance(r, types.FrameType) and r is not referrers
    )


def sweep_stale_settings(retired_epoch: int) -> list[StaleHolder]:
    """Report every live cached settings instance still stamped with ``retired_epoch``.

    Walks the roster for instances whose stamp is the retired epoch and are still
    alive (a holder is keeping them past the cache clear), summarising each via
    ``gc.get_referrers``. Logs ERROR naming the holders — a retired-epoch instance
    still reachable is a stale-config leak, never dropped silently — and returns
    the findings so a probe/e2e can assert zero.
    """
    if retired_epoch >= _current_epoch():
        raise ValueError(
            f"sweep_stale_settings requires a retired epoch, but {retired_epoch} is the current or a future epoch"
        )
    with _roster_lock:
        roster = list(_stamp_roster)
    stale: list[StaleHolder] = []
    for ref in roster:
        instance = ref()
        if instance is None:
            continue
        if getattr(instance, _EPOCH_STAMP_ATTR, None) != retired_epoch:
            continue
        stale.append(
            StaleHolder(
                settings_type=f"{type(instance).__module__}.{type(instance).__qualname__}",
                epoch=retired_epoch,
                holders=_summarize_holders(instance),
            )
        )
    for holder in stale:
        logger.error(
            "Stale settings instance %s (epoch %d) still held by: %s",
            holder.settings_type,
            holder.epoch,
            ", ".join(holder.holders) or "<unknown>",
        )
    return stale
