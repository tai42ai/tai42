"""Fleet-wide mutual exclusion over the skeleton database — the advisory-lock seam.

A boot step that every process of a multi-process deployment runs (``tai serve
--workers N`` plus the backend worker) performs a read-then-write against ONE shared
database. Two such processes racing observe the same "absent" state and both write,
so the outcome depends on the interleaving. This module holds the generic mutex that
makes the read-then-write atomic across processes: a **transaction-scoped**
PostgreSQL advisory lock keyed by a caller-chosen namespace plus a name, held for the
body of an ``async with``.

Transaction scope (``pg_advisory_xact_lock``) over session scope: the lock is
released by the transaction end, and by the connection close on any unwind
(including a cancellation), so a crashed or cancelled holder can never wedge the
fleet — there is no explicit unlock to miss.

The lock is taken on a DEDICATED one-shot pool (``fresh=True``, bounds pinned to 1),
never a shared-pool checkout: the body runs its own store work on the shared pool, so
a shared checkout held for the whole body would consume a connection the body itself
needs (a single-connection deployment would self-deadlock), and a driver error inside
the body cannot evict the shared pool from here.

The two-integer key form is used deliberately: PostgreSQL keeps the ``(int, int)``
advisory-lock key space separate from the single-``bigint`` space, so these locks can
never collide with the migration runner's or the marketplace installer's bigint keys.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.db.discovery import SKELETON_COMPONENT

# The signed 32-bit range PostgreSQL's two-integer advisory-lock keys live in.
_INT32_BYTES = 4


def name_lock_key(name: str) -> int:
    """The stable signed-int32 advisory-lock key for *name*.

    A cryptographic digest, not :func:`hash`: the builtin string hash is salted per
    process, so two workers would derive DIFFERENT keys for the same name and never
    exclude each other.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=_INT32_BYTES).digest()
    return int.from_bytes(digest, "big", signed=True)


@asynccontextmanager
async def advisory_name_lock(namespace: int, name: str) -> AsyncIterator[None]:
    """Hold the fleet-wide ``(namespace, name)`` advisory lock for the context body.

    *namespace* is a signed-int32 constant the calling feature owns, so two features
    locking the same name do not exclude each other. The acquire BLOCKS (it is not a
    ``try``): a second worker waits for the first and then observes the first's
    committed result, which is the whole point — a refusal would leave the waiter's
    work undone. The wait is bounded by the session ``statement_timeout`` the store's
    DSN carries, which surfaces a stuck holder as a loud error rather than a hang.

    The caller must have established that the skeleton store is configured; opening
    the lock connection is what a configured store means, and an unconfigured one
    fails loudly here rather than being silently skipped.
    """
    kwargs = component_store_settings(SKELETON_COMPONENT).client_kwargs()
    kwargs["min_size"] = 1
    kwargs["max_size"] = 1
    async with (
        client_ctx(PostgresClient, fresh=True, **kwargs) as pool,
        pool.connection() as conn,
        conn.transaction(),
    ):
        await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (namespace, name_lock_key(name)))
        yield
