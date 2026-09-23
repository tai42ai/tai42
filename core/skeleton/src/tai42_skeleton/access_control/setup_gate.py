"""The setup-door gate (secure-by-default).

A fresh deployment with access control ON and no principal yet has no authenticated
door to initialize itself. This module is the gate for the public ``POST /api/setup``
door that creates the owner: the effective token is an operator-set ``TAI_SETUP_TOKEN``
or, absent that, an auto-generated token fixed once at startup via ``SET NX`` on the
shared access-control Redis (every worker agrees on one value; only the winner logs it,
and ``NX`` makes that a once-per-deployment line — a later boot finds it set and neither
regenerates nor re-logs). ``TAI_SETUP_OPEN`` disables the gate for local/dev.

The token is READ per request, never generated per request; generation and the single
log line happen once at startup in :func:`ensure_setup_token`.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.management import provider_capabilities
from tai42_skeleton.access_control.settings import (
    AccessControlSettings,
    access_control_settings,
    setup_settings,
)
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger(__name__)


class SetupContendedError(RuntimeError):
    """The mint mutex is held by a concurrent setup.

    Exactly one may initialize, so the loser is turned away as already-initialized.
    """


# The setup door's serviceability conditions, one source shared by the door (the 501 it
# answers) and boot-time token minting (which skips a door that would refuse). Each string
# is the client-facing 501 message, in one voice: the failing condition, then what the door
# needs. Ordered as the door reports them.
_ACCESS_CONTROL_OFF = (
    "the setup door serves access-controlled installs only; it is disabled while "
    "ACCESS_CONTROL_ENABLE is off (with the gate off there is nothing to initialize)"
)
_NO_KEY_MINTING_PROVIDER = (
    "no configured identity provider can mint api keys; the setup door requires a key-minting provider"
)
_ACCESS_CONTROL_REDIS_UNSET = (
    "the access-control Redis is not configured (ACCESS_CONTROL_REDIS_URL / TAI_DEFAULT_REDIS_URL); "
    "the setup door requires it for the setup token, throttle, and mint lock"
)


def setup_unserviceable_reason(settings: AccessControlSettings) -> str | None:
    """The 501 reason the ``POST /api/setup`` door cannot initialize, or ``None`` when it can.

    Serviceable iff access control is on, a configured identity provider can mint api keys
    (the owner needs a first key), and the AC Redis (the home of the setup token, throttle,
    and mint lock) is set. The single source of these conditions: the door raises
    :class:`~tai42_skeleton.operations.errors.NotSupportedError` with this text, and
    boot-time minting skips a door that would refuse so it never reaches for an absent Redis
    (see :func:`ensure_setup_token`).
    """
    if not settings.enable:
        return _ACCESS_CONTROL_OFF
    if not any(mintable for _name, mintable in provider_capabilities()):
        return _NO_KEY_MINTING_PROVIDER
    if not settings.redis.redis_url:
        return _ACCESS_CONTROL_REDIS_UNSET
    return None


async def ensure_setup_token() -> None:
    """Fix the shared auto-token once at startup (no-op when no token is needed).

    Skips entirely when the gate is open, an operator token is set, or the door is not
    serviceable (see :func:`setup_unserviceable_reason`) — so a deployment that does not use
    the feature boots without reaching for an absent Redis. Otherwise each worker attempts
    ``SET key <fresh> NX`` on the shared access-control Redis; the winner fixes the
    effective token and is the only one to log it. ``NX`` makes the fix — and the single
    log line — happen exactly once for the deployment: a later boot finds the token already
    set, so it neither regenerates nor re-logs. Read, never regenerated, on every subsequent
    request.

    While ``TAI_SETUP_OPEN`` is set and no principal exists, the gate is ungated: warn
    loudly every boot so a deployment never silently ships an open setup door.
    """
    settings = access_control_settings()
    setup = setup_settings()
    if setup.open:
        if settings.enable and not await _any_principal_exists_quietly():
            logger.warning(
                "setup: TAI_SETUP_OPEN is set — POST /api/setup accepts ANY token and initializes "
                "the deployment without one; unset it outside local/dev"
            )
        return
    if setup.token is not None:
        return
    # An optional feature's startup work never breaks boot for a deployment that does not
    # use it. Mint the shared auto-token only when the door is SERVICEABLE — access control
    # on, a key-minting identity provider configured, and the AC Redis present. Otherwise
    # the door self-disables at request time (501), so there is nothing to fix at boot.
    reason = setup_unserviceable_reason(settings)
    if reason is not None:
        logger.debug("setup: not minting a startup token — %s", reason)
        return
    candidate = secrets.token_urlsafe(32)
    async with client_ctx(RedisClient, settings.redis) as r:
        won = await awaited(r.set(settings.setup_token_key, candidate, nx=True))
    if won:
        logger.info(
            "setup token: %s — pass it to `tai setup` (or POST /api/setup); only someone who "
            "can read this log can initialize the deployment",
            candidate,
        )


async def _any_principal_exists_quietly() -> bool:
    """Best-effort principal-existence read for the open-window warning; a store fault never breaks boot."""
    from tai42_skeleton.access_control import management

    try:
        return await management.any_principal_exists()
    except Exception:
        logger.debug("setup: could not read principal existence for the open-window warning", exc_info=True)
        return False


async def resolve_setup_token() -> str:
    """The effective setup token when the gate is active — read, never generated.

    An operator-set token is returned directly; otherwise the shared auto-token is read
    from Redis. Its absence while the gate is active RAISES rather than silently opening
    the door.
    """
    setup = setup_settings()
    if setup.token is not None:
        return setup.token.get_secret_value()
    settings = access_control_settings()
    async with client_ctx(RedisClient, settings.redis) as r:
        stored = await awaited(r.get(settings.setup_token_key))
    if not stored:
        raise RuntimeError(
            "setup token invariant breach: the auto-generated setup token is absent from the "
            "access-control Redis while the gate is active (Redis may have been flushed, or this "
            "instance restarted mid-life); restart the deployment to regenerate it"
        )
    # decode_responses yields a str; decode defensively against redis-py's ResponseT.
    return stored.decode() if isinstance(stored, bytes) else stored


async def verify_setup_token(presented: str) -> bool:
    """Whether ``presented`` opens the gate.

    Constant-time compared against the effective token; ``TAI_SETUP_OPEN`` opens the gate for any
    input (local/dev).
    """
    if setup_settings().open:
        return True
    return secrets.compare_digest(presented, await resolve_setup_token())


@asynccontextmanager
async def setup_lock() -> AsyncIterator[None]:
    """Serialize the existence-check-and-owner-mint under one AC-Redis lock.

    Acquires ``setup_lock_key`` with ``SET NX`` (TTL-bounded, so a crashed holder cannot
    deadlock the door forever) and releases it — only if still owned — on exit, so a failed
    setup frees the lock for a clean retry. A caller that cannot acquire it raises
    :class:`SetupContendedError`: another setup is mid-mint, so exactly one ever passes the
    check-and-mint.
    """
    settings = access_control_settings()
    owner = secrets.token_urlsafe(16)
    async with client_ctx(RedisClient, settings.redis) as r:
        acquired = await awaited(r.set(settings.setup_lock_key, owner, nx=True, ex=settings.setup_lock_ttl_seconds))
        if not acquired:
            raise SetupContendedError("a concurrent setup holds the mint lock")
        try:
            yield
        finally:
            stored = await awaited(r.get(settings.setup_lock_key))
            held = stored.decode() if isinstance(stored, bytes) else stored
            if held == owner:
                await awaited(r.delete(settings.setup_lock_key))


async def setup_throttle_locked(client_ip: str) -> bool:
    """Whether wrong-token attempts from ``client_ip`` are currently backed off.

    Checked BEFORE the token is compared, so a locked attempt never reaches the comparison.
    """
    settings = access_control_settings()
    async with client_ctx(RedisClient, settings.redis) as r:
        return bool(await awaited(r.get(f"{settings.setup_throttle_lock_prefix}{client_ip}")))


async def record_setup_failure(client_ip: str) -> None:
    """Count one wrong-token attempt from ``client_ip`` and, past the threshold, arm a backoff lock.

    The backoff is a capped exponential; the counter carries the cap as its TTL, so an IP that
    pauses that long decays back to un-escalated.
    """
    settings = access_control_settings()
    cap = settings.setup_throttle_cap_seconds
    fail_key = f"{settings.setup_throttle_fail_prefix}{client_ip}"
    lock_key = f"{settings.setup_throttle_lock_prefix}{client_ip}"
    async with client_ctx(RedisClient, settings.redis) as r:
        current = await awaited(r.get(fail_key))
        failures = (int(current) if current else 0) + 1
        await awaited(r.set(fail_key, str(failures), ex=cap))
        if failures > settings.setup_throttle_threshold:
            backoff = min(2 ** (failures - settings.setup_throttle_threshold - 1), cap)
            await awaited(r.set(lock_key, "1", ex=backoff))


async def clear_setup_failures(client_ip: str) -> None:
    """Reset an IP's failure counter and backoff lock after a successful setup."""
    settings = access_control_settings()
    async with client_ctx(RedisClient, settings.redis) as r:
        await awaited(
            r.delete(
                f"{settings.setup_throttle_fail_prefix}{client_ip}",
                f"{settings.setup_throttle_lock_prefix}{client_ip}",
            )
        )
