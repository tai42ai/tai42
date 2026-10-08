"""Central registry of cached settings accessors so every cached singleton can be dropped in one call.

``reset_all_settings`` drops every cached settings singleton at once (the
live-reload soft restart re-reads env then resets here).

Each constructed ``BaseSettings`` instance is stamped with the epoch it was born
under and recorded in a weakref roster, so ``sweep_stale_settings`` can find any
instance of a retired epoch that a holder is still keeping alive past a reset — a
stale-config leak, reported loudly and never dropped.
"""

import contextlib
import functools
import gc
import inspect
import logging
import sys
import threading
import types
import weakref
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from pydantic_settings import BaseSettings

from tai42_kit.settings.base import TaiBaseSettings
from tai42_kit.settings.env_file import EnvFileIdentity, env_file_identity

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


def _env_file_token() -> EnvFileIdentity | None:
    path = TaiBaseSettings.tai_env_file
    return None if path is None else env_file_identity(path)


def keyed_settings_cache[K: Hashable, V](fn: Callable[[K], V]) -> Callable[[K], V]:
    """Cache a one-argument settings accessor per key, cleared on reset and re-read when the env file changes.

    An entry is served while the env file keeps the identity it had when the entry
    was built; ``reset_all_settings()`` drops every entry. A settings instance is
    epoch-stamped on construction like :func:`settings_cache`'s. A build that raises
    stores nothing, so the next call builds again.
    """
    entries: dict[K, tuple[EnvFileIdentity | None, V]] = {}

    @functools.wraps(fn)
    def cached(key: K) -> V:
        token = _env_file_token()
        entry = entries.get(key)
        if entry is not None and entry[0] == token:
            return entry[1]
        value = fn(key)
        _stamp_settings(value)
        entries[key] = (token, value)
        return value

    _CACHE_CLEARS[_key(fn)] = entries.clear
    return cached


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
    class, and the type of each object still referencing the instance).
    """

    settings_type: str
    epoch: int
    holders: tuple[str, ...]


def _describe_frame(kind: str, name: str, frame: types.FrameType | None) -> str:
    if frame is None:
        return f"{kind} {name}"
    return f"{kind} {name} ({frame.f_code.co_filename}:{frame.f_lineno})"


def _describe_referrer(referrer: object) -> str:
    # A frame or a suspended coroutine/generator is named by its function and the
    # line it stands at, so an ERROR line points at the code that keeps the instance.
    if isinstance(referrer, types.FrameType):
        return _describe_frame("frame", referrer.f_code.co_qualname, referrer)
    if isinstance(referrer, types.CoroutineType):
        return _describe_frame("coroutine", referrer.cr_code.co_qualname, referrer.cr_frame)
    if isinstance(referrer, types.GeneratorType):
        return _describe_frame("generator", referrer.gi_code.co_qualname, referrer.gi_frame)
    if isinstance(referrer, types.AsyncGeneratorType):
        return _describe_frame("async generator", referrer.ag_code.co_qualname, referrer.ag_frame)
    return f"{type(referrer).__module__}.{type(referrer).__qualname__}"


_SUSPENDABLE_CODE_FLAGS = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR


def _executing_frame_holders(instance: object, sweep_frame: types.FrameType) -> list[str]:
    """Name every executing plain-function frame, on any thread, that has ``instance`` as a local.

    An executing frame is not a gc referrer of its locals, so ``gc.get_referrers``
    cannot see it. Generator and coroutine frames are skipped here: their
    generator/coroutine object is a gc referrer and is named from there. The
    sweep's own frames (``sweep_frame`` and the frames it called) hold the
    instance only to inspect it, so they are skipped too.
    """
    own: list[types.FrameType] = []
    frame: types.FrameType | None = sys._getframe()
    try:
        while frame is not None and frame is not sweep_frame.f_back:
            own.append(frame)
            frame = frame.f_back
        found: list[str] = []
        for top in sys._current_frames().values():
            frame = top
            while frame is not None:
                if (
                    not frame.f_code.co_flags & _SUSPENDABLE_CODE_FLAGS
                    and not any(frame is mine for mine in own)
                    and any(value is instance for value in frame.f_locals.values())
                ):
                    found.append(_describe_frame("frame", frame.f_code.co_qualname, frame))
                frame = frame.f_back
        return found
    finally:
        own.clear()
        del frame


def _summarize_holders(instance: object, sweep_frame: types.FrameType) -> tuple[str, ...]:
    # The referrers list itself is this sweep's own scaffolding, never a holder.
    referrers = gc.get_referrers(instance)
    try:
        held_by = [_describe_referrer(r) for r in referrers if r is not referrers]
    finally:
        del referrers
    return (*held_by, *_executing_frame_holders(instance, sweep_frame))


def _retired_instances(retired_epoch: int) -> list["weakref.ref[BaseSettings]"]:
    with _roster_lock:
        roster = list(_stamp_roster)
    return [ref for ref in roster if getattr(ref(), _EPOCH_STAMP_ATTR, None) == retired_epoch]


def sweep_stale_settings(retired_epoch: int) -> list[StaleHolder]:
    """Report every settings instance stamped with ``retired_epoch`` that something still holds.

    Walks the roster for instances whose stamp is the retired epoch and are still
    alive. When any is found, cyclic garbage is collected first, so an instance
    that only unreachable objects keep (the finished frame of a cancelled task
    awaiting collection) is freed rather than reported. Each instance still alive
    after that is reachable, which is a stale-config leak: it is logged at ERROR
    naming its holders (a referrer's type, or the function and ``file:line`` of a
    frame or coroutine that keeps it) and returned, so a probe/e2e can assert zero.
    Nothing is dropped.
    """
    if retired_epoch >= _current_epoch():
        raise ValueError(
            f"sweep_stale_settings requires a retired epoch, but {retired_epoch} is the current or a future epoch"
        )
    if not _retired_instances(retired_epoch):
        return []
    gc.collect()
    sweep_frame = sys._getframe()
    stale: list[StaleHolder] = []
    for ref in _retired_instances(retired_epoch):
        instance = ref()
        if instance is None:
            continue
        stale.append(
            StaleHolder(
                settings_type=f"{type(instance).__module__}.{type(instance).__qualname__}",
                epoch=retired_epoch,
                holders=_summarize_holders(instance, sweep_frame),
            )
        )
        del instance
    del sweep_frame
    for holder in stale:
        logger.error(
            "Stale settings instance %s (epoch %d) still held by: %s",
            holder.settings_type,
            holder.epoch,
            ", ".join(holder.holders) or "<unknown>",
        )
    return stale
