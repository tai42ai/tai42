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


class _ClientEntry:
    """A pooled client plus its live-lease bookkeeping.

    ``leases`` counts the ``current()`` bodies holding this client right now. A
    client belonging to a retired epoch closes on its last lease release;
    ``closing`` marks the entry as being (or already) torn down so no path closes
    it twice.
    """

    __slots__ = ("client", "closing", "leases")

    def __init__(self, client: object) -> None:
        self.client = client
        self.leases = 0
        self.closing = False


# Clients are cached per event loop (async pools are bound to the loop that
# created them), per epoch, and per PooledClient subclass + connection key. The
# epoch is the outer key: a retired epoch's pools stay reachable until their
# leases drain while new leases pool under the current epoch. Keyed off the loop
# in a WeakKeyDictionary so a loop's caches drop when the loop is collected — no
# attribute is stashed on the loop object. The epoch never enters the string
# connection key (that would break the close(**kwargs) round-trip); it is the
# dict layer above it.
_loop_clients: "WeakKeyDictionary[AbstractEventLoop, dict[int, dict[type, dict[str, _ClientEntry]]]]" = (
    WeakKeyDictionary()
)
_loop_locks: "WeakKeyDictionary[AbstractEventLoop, dict[tuple[int, type, str], asyncio.Lock]]" = WeakKeyDictionary()

# The maps above are module-global and reached from many threads: a template
# render runs in a worker thread (asyncio.to_thread) whose {% include %} bridge
# spins its own loop (asyncio.run), so concurrent renders touch these maps at the
# same time. WeakKeyDictionary is not thread-safe, so every EXPLICIT access to the
# OUTER maps — and every read/write of the epoch counter below — is serialized
# under this lock. The lock is held only around dict bookkeeping and the counter,
# never across an await (client creation/closing happens outside it). The lock is
# non-reentrant: no path holds it while calling a helper that re-acquires it. The
# weak-ref cleanup of a collected loop's entry still fires off-lock on an
# arbitrary thread, but that removal is a single C-level op (atomic under the
# GIL). Inner per-loop dicts stay owned by their single loop.
_registry_lock = threading.Lock()

# The single process-wide epoch counter. It keys the pool maps above and (via
# ``current_client_epoch``) stamps cached settings instances — one counter for
# both, never a second. A plain int guarded by ``_registry_lock`` so a reload
# worker thread can advance it while the serving loop reads it.
_client_epoch = 0

# How long ``drain_epoch`` sleeps between lease-count polls while waiting for a
# retired epoch's leases to reach zero.
_DRAIN_POLL_SECONDS = 0.01

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


def advance_client_epoch() -> int:
    """Advance the process-wide client epoch and return the epoch just retired.

    New ``current()`` leases pool under the new epoch; the retired epoch's pools
    stay live until their last lease releases (or ``drain_epoch`` force-closes
    them at a deadline). The same counter stamps cached settings instances.
    """
    global _client_epoch
    with _registry_lock:
        retired = _client_epoch
        _client_epoch += 1
        return retired


def current_client_epoch() -> int:
    """The current client epoch — the value new leases and settings stamps carry."""
    with _registry_lock:
        return _client_epoch


class PooledClient[T]:
    @staticmethod
    def _key(**kwargs) -> str:
        return json.dumps(kwargs, sort_keys=True)

    def _clients_for_loop(self, loop: AbstractEventLoop, epoch: int) -> dict[str, _ClientEntry]:
        with _registry_lock:
            per_loop = _loop_clients.get(loop)
            if per_loop is None:
                per_loop = {}
                _loop_clients[loop] = per_loop
            per_epoch = per_loop.get(epoch)
            if per_epoch is None:
                per_epoch = {}
                per_loop[epoch] = per_epoch
            return per_epoch.setdefault(self.__class__, {})

    def _create_lock(self, loop: AbstractEventLoop, epoch: int, key: str) -> asyncio.Lock:
        with _registry_lock:
            per_loop = _loop_locks.get(loop)
            if per_loop is None:
                per_loop = {}
                _loop_locks[loop] = per_loop
            lock = per_loop.get((epoch, self.__class__, key))
            if lock is None:
                lock = asyncio.Lock()
                per_loop[(epoch, self.__class__, key)] = lock
            return lock

    def _register(self, loop: AbstractEventLoop, epoch: int, key: str, created: T) -> _ClientEntry | None:
        """Register a freshly created client under ``epoch``, or refuse if retired.

        Returns the new entry, or ``None`` when the epoch advanced during
        ``_create`` — the client then belongs to a now-retired (possibly already
        drained) epoch and must never land there, so the caller closes it and
        retries under the fresh epoch. The epoch check and the write are one
        atomic step under the lock; the current epoch can never be drained, so a
        write here is safe from a concurrent ``drain_epoch``.
        """
        with _registry_lock:
            if epoch != _client_epoch:
                return None
            per_loop = _loop_clients.get(loop)
            if per_loop is None:
                per_loop = {}
                _loop_clients[loop] = per_loop
            per_epoch = per_loop.get(epoch)
            if per_epoch is None:
                per_epoch = {}
                per_loop[epoch] = per_epoch
            clients = per_epoch.get(self.__class__)
            if clients is None:
                clients = {}
                per_epoch[self.__class__] = clients
            entry = _ClientEntry(created)
            clients[key] = entry
            return entry

    async def _acquire(self, loop: AbstractEventLoop, key: str, kwargs: dict) -> tuple[_ClientEntry, int]:
        """Return the pooled entry for ``key`` under the current epoch, creating it once.

        Captures the epoch once and pools consistently under it: a client created
        under epoch N lands in epoch N's dict or is closed-and-retried, never
        orphaned in N+1. Creation is double-checked under a per-(epoch, class, key)
        lock so two concurrent first-use callers build exactly one client.
        """
        while True:
            epoch = current_client_epoch()
            clients = self._clients_for_loop(loop, epoch)
            entry = clients.get(key)
            if entry is not None:
                return entry, epoch
            async with self._create_lock(loop, epoch, key):
                # The captured epoch may have retired while we waited for the lock;
                # retry under the fresh epoch instead of resurrecting the dead
                # epoch's dict (setdefault would re-materialize an emptied pool for
                # a doomed create-and-refuse).
                if current_client_epoch() != epoch:
                    continue
                # Re-fetch the live inner dict now that the lock is held:
                # shutdown_all_clients()/drain_epoch may have detached this
                # epoch's pool between the capture above and acquiring the lock.
                # The re-check and any write below must see the LIVE dict so a
                # second waiter cannot orphan the first waiter's client.
                clients = self._clients_for_loop(loop, epoch)
                entry = clients.get(key)
                if entry is not None:
                    return entry, epoch
                created = await self._create(**kwargs)
                # ``_create`` awaited, so the epoch may have advanced and a
                # shutdown/drain may have detached the pool. ``_register`` re-reads
                # both atomically: it lands the client in the LIVE current-epoch
                # dict, or refuses (epoch retired) so we close and retry rather
                # than orphan it in a dict nothing drains.
                registered = self._register(loop, epoch, key, created)
                if registered is not None:
                    return registered, epoch
            await self._close(created)

    @asynccontextmanager
    async def current(self, **kwargs) -> AsyncIterator[T]:
        loop = asyncio.get_running_loop()
        key = self._key(**kwargs)
        entry, epoch = await self._acquire(loop, key, kwargs)
        entry.leases += 1
        try:
            yield cast("T", entry.client)
        except BaseException as e:
            # Only errors the predicate classifies as a disconnection evict the
            # client; everything else (including cancellation) re-raises untouched.
            if not self._is_disconnection_error(e):
                raise
            # Disconnection eviction is exempt from close-on-last-release: a client
            # classified as disconnected closes immediately regardless of live
            # leases. Concurrent leases surface a retryable ClientDisconnectedError
            # and the re-fetch discipline rebuilds a fresh client.
            if self._detach(loop, epoch, key, entry):
                try:
                    await self._close(cast("T", entry.client))
                except Exception:
                    logger.debug("Failed to close disconnected client %s", type(entry.client).__name__, exc_info=True)
            raise ClientDisconnectedError(
                f"{type(entry.client).__name__} disconnected and was removed from the cache. "
                f"Retry the operation to create a new client. (Original error: {e})"
            ) from e
        finally:
            entry.leases -= 1
            await self._release_if_retired(loop, epoch, key, entry)

    def _detach(self, loop: AbstractEventLoop, epoch: int, key: str, entry: _ClientEntry) -> bool:
        """Mark the entry closing and drop it from its pool; return whether WE closed it.

        Returns ``False`` if another path (drain force-close) already claimed the
        teardown, so the caller does not close a client twice.
        """
        with _registry_lock:
            already = entry.closing
            entry.closing = True
            per_loop = _loop_clients.get(loop)
            per_epoch = per_loop.get(epoch) if per_loop is not None else None
            clients = per_epoch.get(self.__class__) if per_epoch is not None else None
            if clients is not None and clients.get(key) is entry:
                del clients[key]
            return not already

    async def _release_if_retired(self, loop: AbstractEventLoop, epoch: int, key: str, entry: _ClientEntry) -> None:
        """Close a retired epoch's client once its last lease releases.

        A current-epoch client stays pooled; a retired epoch's client closes on
        the release that drops its lease count to zero (unless a drain already
        claimed it).
        """
        with _registry_lock:
            if entry.leases > 0 or entry.closing or epoch == _client_epoch:
                return
            per_loop = _loop_clients.get(loop)
            per_epoch = per_loop.get(epoch) if per_loop is not None else None
            clients = per_epoch.get(self.__class__) if per_epoch is not None else None
            if clients is None or clients.get(key) is not entry:
                return
            entry.closing = True
            del clients[key]
        # A cleanup close failure on lease release must not mask the caller body's
        # in-flight exception (nor inject an error into a clean return); isolate it
        # as the disconnection path and drain/shutdown already do.
        try:
            await self._close(cast("T", entry.client))
        except Exception:
            logger.exception("Error closing retired client %s", type(entry.client).__name__)

    async def close(self, **kwargs) -> None:
        loop = asyncio.get_running_loop()
        epoch = current_client_epoch()
        key = self._key(**kwargs)
        with _registry_lock:
            per_loop = _loop_clients.get(loop)
            per_epoch = per_loop.get(epoch) if per_loop is not None else None
            clients = per_epoch.get(self.__class__) if per_epoch is not None else None
            entry = clients.get(key) if clients is not None else None
            if clients is None or entry is None or entry.closing:
                return
            entry.closing = True
            del clients[key]
        await self._close(cast("T", entry.client))

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


async def drain_epoch(epoch: int, deadline: float) -> None:
    """Drain and close a retired epoch's client pools for the running loop.

    Waits up to ``deadline`` seconds for every leased client of ``epoch`` to reach
    zero leases (each closes itself on its last release), then force-closes any
    that remain regardless of held leases, collecting failures into an
    ``ExceptionGroup`` so none is dropped. Must run on the loop that owns the
    pools; the current epoch cannot be drained.
    """
    loop = asyncio.get_running_loop()
    with _registry_lock:
        if epoch == _client_epoch:
            raise ValueError(f"cannot drain the current client epoch {epoch}")
        per_loop = _loop_clients.get(loop)
        present = per_loop is not None and epoch in per_loop
    if not present:
        return

    end = loop.time() + deadline
    while True:
        with _registry_lock:
            per_loop = _loop_clients.get(loop)
            per_epoch = per_loop.get(epoch) if per_loop is not None else None
            # Poll HELD LEASES, not pooled presence: an idle 0-lease retired client
            # never fires another release event, so waiting on its presence would
            # burn the whole deadline — it must fall straight through to force-close.
            pending = per_epoch is not None and any(
                entry.leases > 0 for clients in per_epoch.values() for entry in clients.values()
            )
        if not pending or loop.time() >= end:
            break
        await asyncio.sleep(_DRAIN_POLL_SECONDS)

    # Detach the epoch atomically, then force-close whatever leases are still
    # held. Detaching first is what makes a concurrent lease release safe: its
    # _release_if_retired finds the epoch gone and closes nothing, so no client is
    # closed twice — the closing flag setters all del under this same lock, so no
    # in-dict entry is ever mid-teardown.
    with _registry_lock:
        per_loop = _loop_clients.get(loop)
        per_epoch = per_loop.pop(epoch, None) if per_loop is not None else None
        locks = _loop_locks.get(loop)
        if locks is not None:
            for lock_key in [k for k in locks if k[0] == epoch]:
                del locks[lock_key]
    if not per_epoch:
        return

    errors = []
    for cls, clients in per_epoch.items():
        closer = cls()
        for entry in list(clients.values()):
            entry.closing = True
            try:
                await closer._close(entry.client)
            except Exception as e:
                logger.exception("Error force-closing client %s", type(entry.client).__name__)
                errors.append(e)
        clients.clear()

    if errors:
        raise ExceptionGroup(f"Errors force-closing client epoch {epoch}", errors)


async def shutdown_all_clients() -> None:
    """Close every live client pool for the running event loop.

    Sweeps every epoch of the per-loop class-keyed cache and closes each pooled
    client — process-teardown semantics, independent of leases. Closes are
    best-effort — failures are collected and raised together, never silently
    dropped.
    """
    # Always called from within a running loop (it is async); get_running_loop
    # raises loudly if that invariant is ever broken.
    loop = asyncio.get_running_loop()
    # Detach this loop's pools atomically, then close their clients without
    # holding the registry lock across the awaits. Detaching first makes a
    # concurrent lease release a no-op (its _release_if_retired finds the pool
    # gone), so no client is closed twice.
    with _registry_lock:
        per_loop = _loop_clients.pop(loop, None)
    if not per_loop:
        return

    errors = []
    for per_epoch in per_loop.values():
        for cls, clients in per_epoch.items():
            closer = cls()
            for entry in list(clients.values()):
                entry.closing = True
                try:
                    await closer._close(entry.client)
                except Exception as e:
                    logger.exception("Error closing client %s", type(entry.client).__name__)
                    errors.append(e)
            clients.clear()

    if errors:
        raise ExceptionGroup("Errors while shutting down clients", errors)
