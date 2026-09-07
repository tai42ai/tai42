"""Fleet-wide mutual exclusion over the skeleton database — the advisory-lock seam.

A read-then-write every process of a multi-process deployment runs against ONE shared
database is atomic only under a lock the whole fleet shares. This module holds that
generic mutex: a transaction-scoped PostgreSQL advisory lock keyed by a caller-chosen
namespace plus a name, held for the body of an ``async with``.

Constraints the code cannot show:

* Transaction scope (``pg_advisory_xact_lock``) releases on the transaction end and on
  the connection close of any unwind, so a crashed or cancelled holder cannot wedge the
  fleet — there is no explicit unlock to miss.
* The lock runs on a DEDICATED one-shot pool, never a shared-pool checkout: the body
  does its own store work on the shared pool, which a checkout held across the body
  would starve (a single-connection deployment self-deadlocks).
* PostgreSQL keeps the ``(int, int)`` advisory-lock key space separate from the
  single-``bigint`` space, so these keys cannot collide with the migration runner's or
  the marketplace installer's bigint keys.
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


def _name_lock_key(name: str) -> int:
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
        await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (namespace, _name_lock_key(name)))
        yield
