"""argon2id password hashing with an off-loop concurrency bound.

argon2 hash/verify are synchronous CPU/memory-bound calls, run off the event loop
under a semaphore; a request that cannot acquire a slot within the wait budget
sheds with :class:`HashCapacityError` rather than queuing or running the hash.
:func:`verify_password` returns ``True`` on match and ``False`` on mismatch; any
other argon2 error propagates.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from tai42_accounts_postgres.settings import accounts_settings

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()

# Verified on the unknown-email path so login timing does not enumerate users.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


class HashCapacityError(Exception):
    """The argon2 concurrency guard could not admit a verify within the wait budget.

    Surfaced by the login route as a 503 load-shed.
    """


def hash_password(password: str) -> str:
    """Return the argon2id hash of ``password`` (library defaults)."""
    return _hasher.hash(password)


async def hash_password_async(password: str) -> str:
    """Hash off the event loop. Ungated — the write paths that call it are gated or
    authed, not an unauthenticated flood vector like the login verify."""
    return await asyncio.to_thread(hash_password, password)


def _verify_sync(hash_: str, password: str) -> bool:
    """Blocking argon2 verify: ``True`` on match, ``False`` on mismatch; other
    argon2 errors propagate (a corrupt hash is not a wrong password)."""
    try:
        return _hasher.verify(hash_, password)
    except VerifyMismatchError:
        return False


class HashGate:
    """A semaphore bounding concurrent off-loop argon2 verifies with load-shed."""

    def __init__(self, concurrency: int, wait_seconds: float) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._wait = wait_seconds

    async def run(self, fn: Callable[..., bool], *args: object) -> bool:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._wait)
        except TimeoutError as exc:
            raise HashCapacityError(
                "login hashing is at capacity; shed this request rather than queue or run the hash"
            ) from exc
        try:
            return await asyncio.to_thread(fn, *args)
        finally:
            self._sem.release()


_gate: HashGate | None = None


def _get_gate() -> HashGate:
    """The process-wide hash gate, built once from the plugin's settings."""
    global _gate
    if _gate is None:
        settings = accounts_settings()
        _gate = HashGate(settings.login_hash_concurrency, settings.login_hash_wait_seconds)
    return _gate


def reset_hash_gate() -> None:
    """Drop the cached gate so the next verify rebuilds it (test isolation)."""
    global _gate
    _gate = None


async def verify_password(hash_: str, password: str) -> bool:
    """Verify ``password`` against ``hash_`` off the event loop, under the gate.

    Raises :class:`HashCapacityError` if the gate is saturated. Returns ``True``
    on match, ``False`` on mismatch; other argon2 errors propagate.
    """
    return await _get_gate().run(_verify_sync, hash_, password)
