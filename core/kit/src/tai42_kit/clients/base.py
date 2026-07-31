import asyncio
import json
import logging
import threading
from asyncio import AbstractEventLoop
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from weakref import WeakKeyDictionary

from tai42_contract.errors import ClientDisconnectedError

logger = logging.getLogger(__name__)

# Clients are cached per event loop (async pools are bound to the loop that
# created them) and per PooledClient subclass + connection key. Keyed off the
# loop in a WeakKeyDictionary so a loop's caches drop when the loop is collected —
# no attribute is stashed on the loop object.
_loop_clients: "WeakKeyDictionary[AbstractEventLoop, dict[type, dict[str, object]]]" = WeakKeyDictionary()
_loop_locks: "WeakKeyDictionary[AbstractEventLoop, dict[tuple[type, str], asyncio.Lock]]" = WeakKeyDictionary()

# The two maps above are module-global and reached from many threads: a template
# render runs in a worker thread (asyncio.to_thread) whose {% include %} bridge
# spins its own loop (asyncio.run), so concurrent renders touch these maps at the
# same time. WeakKeyDictionary is not thread-safe, so every EXPLICIT access to the
# OUTER maps is serialized under this lock. The lock is held only around dict
# bookkeeping, never across an await (client creation/closing happens outside it).
# The weak-ref cleanup of a collected loop's entry still fires off-lock on an
# arbitrary thread, but that removal is a single C-level op (atomic under the
# GIL). Inner per-loop dicts stay owned by their single loop.
_registry_lock = threading.Lock()

# RuntimeError messages asyncio emits when a loop-bound resource is used after
# its event loop closed or from a different loop. Driver stacks that surface a
# dead session as a plain RuntimeError (curl_cffi, anyio) produce one of these;
# any other RuntimeError comes from caller code and must propagate untouched.
_LOOP_BOUND_ERROR_MARKERS = (
    "Event loop is closed",
    "attached to a different loop",
    "bound to a different event loop",
)


def is_loop_bound_runtime_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a RuntimeError signalling a closed/mismatched event loop."""
    return isinstance(exc, RuntimeError) and any(marker in str(exc) for marker in _LOOP_BOUND_ERROR_MARKERS)


def reject_unknown_connection_kwargs(client_label: str, kwargs: dict[str, object], allowed: frozenset[str]) -> None:
    """Reject connection kwargs a client does not understand, raising ``ValueError``.

    A pooled client keys its per-loop pool on the full kwargs dict, so an
    unrecognized kwarg (typically a typo) would otherwise split the pool key and
    silently create a second, mis-configured client rather than the caller's
    intended one. The shared validation convention across the concrete impls:
    fail loudly, naming the offending keys and the allowed set.
    """
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise ValueError(f"{client_label} received unknown connection kwarg(s) {unknown}; allowed: {sorted(allowed)}")


class PooledClient[T]:
    @staticmethod
    def _key(**kwargs) -> str:
        return json.dumps(kwargs, sort_keys=True)

    def _clients_for_loop(self, loop: AbstractEventLoop) -> dict[str, T]:
        with _registry_lock:
            per_loop = _loop_clients.get(loop)
            if per_loop is None:
                per_loop = {}
                _loop_clients[loop] = per_loop
            # The registry is heterogeneous across subclasses (values typed
            # ``object``); a single class's entries are all ``T``.
            return cast("dict[str, T]", per_loop.setdefault(self.__class__, {}))

    def _create_lock(self, loop: AbstractEventLoop, key: str) -> asyncio.Lock:
        with _registry_lock:
            per_loop = _loop_locks.get(loop)
            if per_loop is None:
                per_loop = {}
                _loop_locks[loop] = per_loop
            lock = per_loop.get((self.__class__, key))
            if lock is None:
                lock = asyncio.Lock()
                per_loop[(self.__class__, key)] = lock
            return lock

    @asynccontextmanager
    async def current(self, **kwargs) -> AsyncIterator[T]:
        loop = asyncio.get_running_loop()
        clients_for_loop = self._clients_for_loop(loop)
        key = self._key(**kwargs)
        # Double-checked creation under a per-(class, key) lock: without it two
        # concurrent first-use callers both create a client and one is orphaned
        # (never closed). The synchronous get/setdefault above never awaits, so
        # the cache and lock lookups are themselves race-free on the single loop.
        if key not in clients_for_loop:
            async with self._create_lock(loop, key):
                # Re-fetch the live inner dict now that the lock is held.
                # ``shutdown_all_clients()`` may have popped this loop's pool out
                # of ``_loop_clients`` — detaching and clearing the dict captured
                # before the lock — between that capture and acquiring the lock.
                # Both the re-check and the write below must see the LIVE dict: a
                # second waiter re-checking the stale-cleared dict would find
                # ``key`` absent, create a duplicate, and overwrite the first
                # waiter's client in the re-registered dict — orphaning it (never
                # closed). The re-fetch and re-check are synchronous, so on the
                # single loop nothing interleaves between them.
                clients_for_loop = self._clients_for_loop(loop)
                if key not in clients_for_loop:
                    created = await self._create(**kwargs)
                    # ``_create`` awaited above, so a shutdown may have detached
                    # the pool again. Re-fetch once more so the write lands in the
                    # live (re-registered if it was swept) dict rather than a
                    # stale one, where a future shutdown/close can reach it. The
                    # re-fetch and write are synchronous — nothing interleaves.
                    clients_for_loop = self._clients_for_loop(loop)
                    clients_for_loop[key] = created

        client = clients_for_loop[key]
        try:
            yield client
        except BaseException as e:
            # Only errors the predicate classifies as a disconnection evict the
            # client; everything else (including cancellation) re-raises
            # untouched.
            if not self._is_disconnection_error(e):
                raise
            clients_for_loop.pop(key, None)
            try:
                await self._close(client)
            except Exception:
                logger.debug("Failed to close disconnected client %s", type(client).__name__, exc_info=True)
            raise ClientDisconnectedError(
                f"{type(client).__name__} disconnected and was removed from the cache. "
                f"Retry the operation to create a new client. (Original error: {e})"
            ) from e

    async def close(self, **kwargs) -> None:
        loop = asyncio.get_running_loop()
        clients_for_loop = self._clients_for_loop(loop)

        key = self._key(**kwargs)
        client = clients_for_loop.pop(key, None)
        if client is not None:
            await self._close(client)

    async def _create(self, **kwargs) -> T:
        raise NotImplementedError

    async def _close(self, client: T) -> None:
        raise NotImplementedError

    def _disconnection_exceptions(self) -> tuple[type[BaseException], ...]:
        # Cancellation is deliberately excluded: a cancelled ``current()`` body
        # must re-raise ``CancelledError`` so the await point unwinds, never be
        # converted into ``ClientDisconnectedError``. Concrete clients override
        # this with their driver's real disconnection set.
        return (ConnectionError,)

    def _is_disconnection_error(self, exc: BaseException) -> bool:
        # Predicate ``current()`` consults to decide whether an error from the
        # body means the pooled client is dead (evict + close + wrap in
        # ``ClientDisconnectedError``) or is an ordinary caller error
        # (propagate untouched). Defaults to an isinstance check against
        # ``_disconnection_exceptions()``; clients whose drivers signal
        # disconnection through a broad exception type (e.g. a plain
        # ``RuntimeError``) override this to also match by message.
        return isinstance(exc, self._disconnection_exceptions())


async def shutdown_all_clients() -> None:
    """Close every live client pool for the running event loop.

    Sweeps the per-loop class-keyed cache and closes each pooled client. Closes
    are best-effort — failures are collected and raised together, never silently
    dropped.
    """
    # Always called from within a running loop (it is async); get_running_loop
    # raises loudly if that invariant is ever broken.
    loop = asyncio.get_running_loop()
    # Detach this loop's pool atomically, then close its clients without holding
    # the registry lock across the awaits.
    with _registry_lock:
        per_loop = _loop_clients.pop(loop, None)
    if not per_loop:
        return

    errors = []
    for cls, clients in list(per_loop.items()):
        closer = cls()
        for client in list(clients.values()):
            try:
                await closer._close(client)
            except Exception as e:
                logger.exception("Error closing client %s", type(client).__name__)
                errors.append(e)
        clients.clear()

    if errors:
        raise ExceptionGroup("Errors while shutting down clients", errors)
