"""The first-key bootstrap gate (secure-by-default).

A fresh deployment with access control ON and no key yet has no authenticated door
to mint its first admin key. This module is the gate for the public
``/api/keys/bootstrap`` door that mints it: the effective token is an operator-set
``ACCESS_CONTROL_BOOTSTRAP_TOKEN`` or, absent that, an auto-generated token fixed once
at startup via ``SET NX`` on the shared access-control Redis (every worker agrees on
one value; only the winner logs it, and ``NX`` makes that a once-per-deployment line —
a later boot finds it set and neither regenerates nor re-logs). ``bootstrap_open``
disables the gate for local/dev.

The token is READ per request, never generated per request; generation and the single
log line happen once at startup in :func:`ensure_bootstrap_token`.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.management import provider_capabilities
from tai42_skeleton.access_control.settings import AccessControlSettings, access_control_settings
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger(__name__)


class BootstrapContended(RuntimeError):
    """The mint mutex is held by a concurrent bootstrap — exactly one may mint, so the
    loser is turned away as already-initialized."""


def _bootstrap_serviceable(settings: AccessControlSettings) -> bool:
    """Whether the ``/api/keys/bootstrap`` door can actually mint, mirroring the door's
    own self-disable checks so boot never mints a token for a door that would 501/403.

    Serviceable iff access control is on, a configured identity provider can mint api
    keys, and the AC Redis (the home of the token, throttle, and mint lock) is set. A
    missing piece is logged, never raised: the door already refuses cleanly at request
    time, so the startup handler does nothing for a deployment that does not use it."""
    if not settings.enable:
        return False
    if not any(mintable for _name, mintable in provider_capabilities()):
        logger.debug(
            "first-key bootstrap: no configured identity provider can mint api keys; "
            "the bootstrap door is disabled, nothing to mint at startup"
        )
        return False
    if not settings.redis.redis_url:
        logger.debug(
            "first-key bootstrap: the access-control Redis is not configured "
            "(ACCESS_CONTROL_REDIS_URL / TAI_DEFAULT_REDIS_URL); the bootstrap door is "
            "unavailable, nothing to mint at startup"
        )
        return False
    return True


async def ensure_bootstrap_token() -> None:
    """Fix the shared auto-token once at startup (no-op when no token is needed).

    Skips entirely when the gate is open, an operator token is set, or the door is not
    serviceable (see :func:`_bootstrap_serviceable`) — so a deployment that does not use
    the feature boots without reaching for an absent Redis. Otherwise each worker
    attempts ``SET key <fresh> NX`` on the shared access-control Redis; the winner
    fixes the effective token and is the only one to log it. ``NX`` makes the fix — and
    the single log line — happen exactly once for the deployment: a later boot finds the
    token already set, so it neither regenerates nor re-logs. Read, never regenerated, on
    every subsequent request.
    """
    settings = access_control_settings()
    if settings.bootstrap_open or settings.bootstrap_token is not None:
        return
    # An optional feature's startup work never breaks boot for a deployment that does
    # not use it. Mint the shared auto-token only when the door is SERVICEABLE — access
    # control on, a key-minting identity provider configured, and the AC Redis (which
    # holds the token, throttle, and mint lock) present. Otherwise the door self-disables
    # at request time (501 / 403), so there is nothing to fix at boot: log and return
    # rather than reach for a Redis that is not there.
    if not _bootstrap_serviceable(settings):
        return
    candidate = secrets.token_urlsafe(32)
    async with client_ctx(RedisClient, settings.redis) as r:
        won = await awaited(r.set(settings.bootstrap_token_key, candidate, nx=True))
    if won:
        logger.info(
            "first-key bootstrap token: %s — pass it to `tai keys bootstrap` (or POST "
            "/api/keys/bootstrap); only someone who can read this log can mint the first admin key",
            candidate,
        )


async def resolve_bootstrap_token() -> str:
    """The effective bootstrap token when the gate is active — read, never generated.

    An operator-set token is returned directly; otherwise the shared auto-token is read
    from Redis. Its absence while the gate is active RAISES rather than silently opening
    the door.
    """
    settings = access_control_settings()
    if settings.bootstrap_token is not None:
        return settings.bootstrap_token.get_secret_value()
    async with client_ctx(RedisClient, settings.redis) as r:
        stored = await awaited(r.get(settings.bootstrap_token_key))
    if not stored:
        raise RuntimeError(
            "bootstrap token invariant breach: the auto-generated first-key token is absent from "
            "the access-control Redis while the gate is active (Redis may have been flushed, or this "
            "instance restarted mid-life); restart the deployment to regenerate it"
        )
    # decode_responses yields a str; decode defensively against redis-py's ResponseT.
    return stored.decode() if isinstance(stored, bytes) else stored


async def verify_bootstrap_token(presented: str) -> bool:
    """Whether ``presented`` opens the gate. Constant-time compared against the
    effective token; ``bootstrap_open`` opens the gate for any input (local/dev)."""
    if access_control_settings().bootstrap_open:
        return True
    return secrets.compare_digest(presented, await resolve_bootstrap_token())


@asynccontextmanager
async def bootstrap_mint_lock() -> AsyncIterator[None]:
    """Serialize the existence-check-and-mint of the first key under one AC-Redis lock.

    Acquires ``bootstrap_lock_key`` with ``SET NX`` (TTL-bounded, so a crashed holder
    cannot deadlock the door forever) and releases it — only if still owned — on exit,
    so a failed mint frees the lock for a clean retry. A caller that cannot acquire it
    raises :class:`BootstrapContended`: another bootstrap is mid-mint, so exactly one
    ever passes the check-and-mint.
    """
    settings = access_control_settings()
    owner = secrets.token_urlsafe(16)
    async with client_ctx(RedisClient, settings.redis) as r:
        acquired = await awaited(
            r.set(settings.bootstrap_lock_key, owner, nx=True, ex=settings.bootstrap_lock_ttl_seconds)
        )
        if not acquired:
            raise BootstrapContended("a concurrent first-key bootstrap holds the mint lock")
        try:
            yield
        finally:
            stored = await awaited(r.get(settings.bootstrap_lock_key))
            held = stored.decode() if isinstance(stored, bytes) else stored
            if held == owner:
                await awaited(r.delete(settings.bootstrap_lock_key))


async def bootstrap_throttle_locked(client_ip: str) -> bool:
    """Whether wrong-token attempts from ``client_ip`` are currently backed off. Checked
    BEFORE the token is compared, so a locked attempt never reaches the comparison."""
    settings = access_control_settings()
    async with client_ctx(RedisClient, settings.redis) as r:
        return bool(await awaited(r.get(f"{settings.bootstrap_throttle_lock_prefix}{client_ip}")))


async def record_bootstrap_failure(client_ip: str) -> None:
    """Count one wrong-token attempt from ``client_ip`` and, past the threshold, arm a
    capped exponential backoff lock. The counter carries the cap as its TTL, so an IP
    that pauses that long decays back to un-escalated."""
    settings = access_control_settings()
    cap = settings.bootstrap_throttle_cap_seconds
    fail_key = f"{settings.bootstrap_throttle_fail_prefix}{client_ip}"
    lock_key = f"{settings.bootstrap_throttle_lock_prefix}{client_ip}"
    async with client_ctx(RedisClient, settings.redis) as r:
        current = await awaited(r.get(fail_key))
        failures = (int(current) if current else 0) + 1
        await awaited(r.set(fail_key, str(failures), ex=cap))
        if failures > settings.bootstrap_throttle_threshold:
            backoff = min(2 ** (failures - settings.bootstrap_throttle_threshold - 1), cap)
            await awaited(r.set(lock_key, "1", ex=backoff))


async def clear_bootstrap_failures(client_ip: str) -> None:
    """Reset an IP's failure counter and backoff lock after a successful mint."""
    settings = access_control_settings()
    async with client_ctx(RedisClient, settings.redis) as r:
        await awaited(
            r.delete(
                f"{settings.bootstrap_throttle_fail_prefix}{client_ip}",
                f"{settings.bootstrap_throttle_lock_prefix}{client_ip}",
            )
        )
