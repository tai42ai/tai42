"""The marketplace operation's concurrency layer.

Two layers, each honest about what it covers:

- The fleet-wide **PostgreSQL advisory lock** (:func:`fleet_lock`) is the
  correctness layer for the VENV: it serializes marketplace operations across
  every worker PROCESS, so two pips never mutate one venv at once. It is held
  across the ENTIRE operation (pip run, manifest apply, attribution write). The
  manifest change's own cross-process atomicity is owned by the pipeline's
  transaction, not this lock.
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

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.marketplace.errors import OperationInProgressError

logger = logging.getLogger(__name__)

# Per-worker fast-path lock: a second same-worker operation is refused
# immediately rather than queued. NOT the correctness layer — each uvicorn worker
# is a separate process with its own instance of this.
operation_lock = asyncio.Lock()

# Fixed key for the fleet-wide session advisory lock that serializes marketplace
# operations. Session- and transaction-scoped advisory locks share ONE key space
# in PostgreSQL, and this feature's DSN commonly targets the same database as the
# connector store (which takes ``0x7461695F636F6E6E`` for category creation), so
# this MUST be a distinct value or the two would block against each other across
# the fleet. "tai42_mktp" as ASCII bytes; the high byte 0x74 keeps it a positive
# bigint.
MARKETPLACE_LOCK_KEY = 0x7461695F6D6B7470  # "tai42_mktp"


@asynccontextmanager
async def fleet_lock() -> AsyncIterator[None]:
    """Hold the fleet-wide marketplace advisory lock for the context body.

    Opens a DEDICATED one-shot ``PostgresClient`` (``fresh=True``, pool bounds
    pinned to 1) rather than a shared-pool checkout: the kit's fresh path closes
    the dedicated pool and its connection on ANY exit — normal return, exception,
    or a ``CancelledError`` from a client disconnect during the minutes-long pip
    run — so the session-scoped lock releases deterministically with the
    connection. A shared checkout would only RETURN the connection on exit (never
    close it), and a cancellation skipping the explicit unlock would put a
    still-locked session back in the shared pool, wedging the fleet with 503s
    until restart.

    The connection is set to autocommit BEFORE the try-lock: psycopg defaults
    autocommit off, so otherwise the lock SELECT would open a transaction idling
    for the whole pip run, and a managed-PG ``idle_in_transaction_session_timeout``
    would kill the session mid-install and silently drop the lock. Autocommit
    closes that hole but NOT the idle-SESSION one: a managed-PG
    ``idle_session_timeout`` or a load-balancer idle drop can still kill the whole
    session mid-pip and release the lock while pip is still mutating the venv — an
    ACCEPTED residual risk whose mitigation is to disable ``idle_session_timeout``
    (and keep TCP keepalives on) for this DSN so a long pip run never trips a
    cutoff.

    ``pg_try_advisory_lock`` returning false means another worker holds it →
    :class:`OperationInProgressError` (retriable 503). The ``pg_advisory_unlock``
    in the ``finally`` is the polite release; the connection close is the
    guarantee, so its failure on a possibly-dead connection is logged and
    suppressed, never allowed to mask the body's original error. A crashed or
    cancelled worker therefore cannot leave the fleet wedged.

    Side benefit of the dedicated client: a psycopg ``OperationalError`` raised
    inside the body stays on this client and cannot evict the SHARED pool through
    the kit's disconnection rewrap.
    """
    kwargs = component_store_settings(SKELETON_COMPONENT).client_kwargs()
    kwargs["min_size"] = 1
    kwargs["max_size"] = 1
    async with (
        client_ctx(PostgresClient, fresh=True, **kwargs) as pool,
        pool.connection() as conn,
    ):
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (MARKETPLACE_LOCK_KEY,))
            row = await cur.fetchone()
        if not (row and row[0]):
            raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
        try:
            yield
        finally:
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT pg_advisory_unlock(%s)", (MARKETPLACE_LOCK_KEY,))
            except Exception:
                logger.warning(
                    "marketplace: advisory unlock failed; the connection close releases the lock", exc_info=True
                )


@asynccontextmanager
async def operation_guard(
    fleet: Callable[[], AbstractAsyncContextManager[None]],
) -> AsyncIterator[None]:
    """Acquire the per-worker lock then the fleet lock, both for the body.

    Refuses immediately (no store/registry/pip/manifest call) when either lock is
    held elsewhere. ``fleet`` is the fleet-lock factory the flow injected, so a
    test drives the guard against a fake fleet lock.
    """
    if operation_lock.locked():
        raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
    async with operation_lock, fleet():
        yield
