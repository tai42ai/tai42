"""The marketplace operation's concurrency layer.

Two layers, each honest about what it covers:

- The fleet-wide **PostgreSQL advisory lock** (:func:`fleet_lock`) is the
  correctness layer for the PLUGINS DIRECTORY: it serializes marketplace operations
  across every worker PROCESS and replica, so two builds never change one directory at
  once. It is held across the ENTIRE operation. While the operation changes the
  directory and the install store (build, swap, manifest apply, attribution write) it
  is held EXCLUSIVELY; once those changes are made, an operation that rolls a fleet
  recycle downgrades it to SHARED on the same session (:meth:`FleetHold.downgrade`)
  and rolls, so a worker the roll restarts can verify the finished directory while a
  second operation is still refused. The manifest change's own cross-process atomicity
  is owned by the pipeline's transaction, not this lock.
- The same advisory key taken in SHARED mode (:func:`fleet_lock_shared`) is held by
  a reader of the plugins directory (the once-per-process boot verification and
  ``tai doctor``): it waits while an operation changes the directory, so it never reads
  a half-changed one, is granted beside an operation that has downgraded to roll, and
  holds a writer off only while it reads.
- The per-worker :data:`operation_lock` is the UX fast path: a second request
  arriving in the SAME worker while an operation runs is refused immediately with
  a retriable :class:`OperationInProgressError` instead of queueing for minutes.
  It is a fast path only — never the mutual-exclusion story, since each worker is
  a separate process with its own copy of it.

:func:`operation_guard` combines the two — the per-worker fast-path refusal then
the fleet lock — as the single door every mutating flow enters through.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from tai42_kit.clients.impl.postgres import pinned_connection
from tai42_kit.db import component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.db.lock_keys import MARKETPLACE_LOCK_KEY
from tai42_skeleton.marketplace.errors import OperationInProgressError

logger = logging.getLogger(__name__)

# Per-worker fast-path lock: a second same-worker operation is refused
# immediately rather than queued. NOT the correctness layer — each uvicorn worker
# is a separate process with its own instance of this.
operation_lock = asyncio.Lock()


class FleetHold(Protocol):
    """A held fleet lock: exclusive until :meth:`downgrade`, shared after it."""

    async def downgrade(self) -> None:
        """Hold the key shared instead of exclusively, with no moment where neither is held."""
        ...


class _AdvisoryHold:
    """The marketplace advisory key held by one pinned session, exclusively or, once downgraded, shared."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.shared = False

    async def downgrade(self) -> None:
        """Take the key shared on the same session, then drop the exclusive hold.

        A session that holds a key exclusively is granted it shared at once, so between the two
        statements the session holds both and no other session can take the key exclusively;
        after them it holds only the shared hold, which a reader's shared request is granted
        beside and another operation's exclusive try-lock is refused by. A hold already shared
        stays as it is.
        """
        if self.shared:
            return
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock_shared(%s)", (MARKETPLACE_LOCK_KEY,))
            await cur.execute("SELECT pg_advisory_unlock(%s)", (MARKETPLACE_LOCK_KEY,))
        self.shared = True

    async def release(self) -> None:
        """Release the hold in the mode it is held in."""
        unlock = "pg_advisory_unlock_shared" if self.shared else "pg_advisory_unlock"
        async with self._conn.cursor() as cur:
            await cur.execute(f"SELECT {unlock}(%s)", (MARKETPLACE_LOCK_KEY,))


@asynccontextmanager
async def fleet_lock() -> AsyncIterator[FleetHold]:
    """Hold the fleet-wide marketplace advisory lock for the context body.

    The body receives the :class:`FleetHold`: exclusive, until the body downgrades it to
    shared to roll a fleet recycle once its changes are made.

    Opens a DEDICATED one-connection pool through the kit's ``pinned_connection``
    seam rather than a shared-pool checkout: the seam closes the dedicated pool and
    its connection on ANY exit — normal return, exception, or a ``CancelledError``
    from a client disconnect during the minutes-long pip run — so the session-scoped
    lock releases deterministically with the connection. A shared checkout would only
    RETURN the connection on exit (never
    close it), and a cancellation skipping the explicit unlock would put a
    still-locked session back in the shared pool, wedging the fleet with 503s
    until restart.

    The connection is set to autocommit BEFORE the try-lock: psycopg defaults
    autocommit off, so otherwise the lock SELECT would open a transaction idling
    for the whole pip run, and a managed-PG ``idle_in_transaction_session_timeout``
    would kill the session mid-install and silently drop the lock. Autocommit
    closes that hole but NOT the idle-SESSION one: a managed-PG
    ``idle_session_timeout`` or a load-balancer idle drop can still kill the whole
    session mid-pip and release the lock while pip is still writing a build — an
    ACCEPTED residual risk whose mitigation is to disable ``idle_session_timeout``
    (and keep TCP keepalives on) for this DSN so a long pip run never trips a
    cutoff.

    ``pg_try_advisory_lock`` returning false means another session holds it in either
    mode → :class:`OperationInProgressError` (retriable 503). The unlock of the mode
    held (``pg_advisory_unlock``, or ``pg_advisory_unlock_shared`` once downgraded) in
    the ``finally`` is the polite release; the connection close is the
    guarantee, so its failure on a possibly-dead connection is logged and
    suppressed, never allowed to mask the body's original error. A crashed or
    cancelled worker therefore cannot leave the fleet wedged.

    Side benefit of the dedicated client: a psycopg ``OperationalError`` raised
    inside the body stays on this client and cannot evict the SHARED pool through
    the kit's disconnection rewrap.
    """
    async with pinned_connection(component_store_settings(SKELETON_COMPONENT)) as conn:
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (MARKETPLACE_LOCK_KEY,))
            row = await cur.fetchone()
        if not (row and row[0]):
            raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
        hold = _AdvisoryHold(conn)
        try:
            yield hold
        finally:
            try:
                await hold.release()
            except Exception:
                logger.warning(
                    "marketplace: advisory unlock failed; the connection close releases the lock", exc_info=True
                )


@asynccontextmanager
async def fleet_lock_shared() -> AsyncIterator[None]:
    """Hold the marketplace advisory key in shared mode for the context body, waiting for a writer.

    ``pg_advisory_lock_shared`` blocks while an operation holds the key exclusively
    (:func:`fleet_lock`) and is compatible with other shared holders, an operation that
    downgraded its hold to roll a fleet recycle included. The connection is
    the same dedicated autocommit one :func:`fleet_lock` opens, for the same reasons: the
    close on any exit is what releases the lock, and no transaction idles while it is held.
    """
    async with pinned_connection(component_store_settings(SKELETON_COMPONENT)) as conn:
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock_shared(%s)", (MARKETPLACE_LOCK_KEY,))
        try:
            yield
        finally:
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT pg_advisory_unlock_shared(%s)", (MARKETPLACE_LOCK_KEY,))
            except Exception:
                logger.warning(
                    "marketplace: shared advisory unlock failed; the connection close releases the lock",
                    exc_info=True,
                )


@asynccontextmanager
async def operation_guard(
    fleet: Callable[[], AbstractAsyncContextManager[FleetHold]],
) -> AsyncIterator[FleetHold]:
    """Acquire the per-worker lock then the fleet lock, both for the body, which receives the fleet hold.

    Refuses immediately (no store/registry/pip/manifest call) when either lock is
    held elsewhere. Both stay held to the body's end, a downgraded fleet hold included, so
    a second operation on this worker or any other is refused until then. ``fleet`` is the
    fleet-lock factory the flow injected, so a test drives the guard against a fake fleet lock.
    """
    if operation_lock.locked():
        raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
    async with operation_lock, fleet() as hold:
        yield hold
