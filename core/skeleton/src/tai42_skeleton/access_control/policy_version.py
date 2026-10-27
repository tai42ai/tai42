"""Invalidation of the access-control policy cache through the policy-version counter.

The access-control caches are keyed on the plain-Redis ``ac:policy_version`` counter. A
committed policy change bumps the counter, forcing every worker's next read to miss its
cache. :func:`record_policy_change` is the call every policy writer makes after its
change; inside a :func:`policy_write_batch` the changes of a run of writes collapse into one
bump when the batch ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.policy_version_scope import remember_policy_version
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.utils.redis_typing import awaited


@dataclass
class _Batch:
    changed: bool = False


_policy_write_batch: ContextVar[_Batch | None] = ContextVar("policy_write_batch", default=None)


@asynccontextmanager
async def policy_write_batch() -> AsyncIterator[None]:
    """Run a run of policy writes under ONE cache invalidation, bumped on exit if anything changed.

    Inside the block every policy writer records its change instead of bumping; the
    one bump runs in a ``finally``, so a raise part-way still invalidates what was written.
    A revoke's bump is not deferred (its fail-closed order needs it immediately). Nested use
    raises ``RuntimeError``.
    """
    if _policy_write_batch.get() is not None:
        raise RuntimeError("policy_write_batch is not reentrant")
    batch = _Batch()
    token = _policy_write_batch.set(batch)
    try:
        yield
    finally:
        _policy_write_batch.reset(token)
        if batch.changed:
            await bump_policy_version()


async def record_policy_change() -> None:
    """Invalidate the policy cache after a committed enforced-authority change.

    Bumps the policy version now, or — inside a :func:`policy_write_batch` — once when the
    batch ends.
    """
    batch = _policy_write_batch.get()
    if batch is not None:
        batch.changed = True
        return
    await bump_policy_version()


async def bump_policy_version() -> int | None:
    """Increment the policy-version counter (plain Redis), forcing a cross-worker cache miss on the next read.

    Returns the new version. A failed bump RAISES loudly — it is never swallowed. Inside an
    open request scope the new version replaces the remembered one, so the rest of that
    access-control decision reads what this bump wrote.

    With access control off (``ACCESS_CONTROL_ENABLE=false``) nothing reads the version, so
    there is nothing to invalidate: the bump touches no Redis and returns ``None``. Turning
    the gate on is a settings change, and the settings reset drops every cache keyed on the
    version, so no decision cached before the gate went off can be served after it is back.
    """
    s = access_control_settings()
    if not s.enable:
        return None
    async with client_ctx(RedisClient, s.redis) as r:
        version = await awaited(r.incr(s.policy_version_key))
    remember_policy_version(version)
    return version
