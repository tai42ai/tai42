"""A failures-only brute-force backoff on the pair-code REDEEM path.

A pair code is ~41 bits and :meth:`ConversationPairCodeStore.redeem` is target-unscoped, so
without a guard a source could grind live codes and merge into a victim's person, defeating
the explicit-consent gate. This throttle bounds that: per ``(target, source)`` — where the
``source`` is the door's ACCOUNTABLE party (the caller keys it on the authenticated api
caller or the provider-attested channel ``cap_key``, NOT a caller-composed conversation
address the attacker can rotate per attempt) — a run of INVALID redeems escalates a capped
exponential backoff, and while locked a redeem is refused WITHOUT touching the code store —
so a lucky guess made under the lock is never burned. A VALID redeem clears the counter, so
an honest user is never blocked, and a throttled redeem returns the platform's SAME uniform
invalid-code reply (D11 no-oracle: the lock is invisible, it never reveals whether a code
was real).

Same posture and shape as the login-failure throttle: Redis-backed (durable and shared
across workers, because brute force is patient), plain ``INCR``/``EXPIRE``/``SET`` with the
benign-race tolerance a counter allows, and it lives ENTIRELY behind the multichannel gate —
its caller only reaches it for a classified redeem on a multichannel-on target, so an
unlinked or multichannel-off conversation is byte-identical to today.
"""

from __future__ import annotations

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.persons import PairingTarget
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited

_NO_BACKEND = "conversation redeem throttling requires the redis conversations backend"


class ConversationRedeemThrottle:
    """The Redis-backed per-(target, source) redeem backoff. Construction refuses with a loud
    501 without the redis conversations backend — a brute-force guard must not live in
    per-worker state a restart clears."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def is_locked(self, target: PairingTarget, source_key: str) -> bool:
        """Whether this source's redeems are currently backed off for ``target``. Checked
        BEFORE the code store is touched, so a locked attempt burns no code."""
        lock_key = self.settings.redeem_lock_key(target.target_kind, target.target_name, source_key)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return bool(await awaited(r.exists(lock_key)))

    async def record_failure(self, target: PairingTarget, source_key: str) -> None:
        """Count one invalid redeem and, past the threshold, (re)arm the backoff lock with
        capped exponential duration. The counter's own TTL is the cap, so a source that stops
        for that long decays back to un-escalated."""
        cap = self.settings.redeem_backoff_cap_seconds
        threshold = self.settings.redeem_backoff_threshold
        fail_key = self.settings.redeem_fail_key(target.target_kind, target.target_name, source_key)
        lock_key = self.settings.redeem_lock_key(target.target_kind, target.target_name, source_key)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            failures = int(await awaited(r.incr(fail_key)))
            await awaited(r.expire(fail_key, cap))
            if failures > threshold:
                # First lock lands the attempt after the threshold is crossed: exponent
                # starts at zero (1 s), doubling each further failure, capped.
                backoff = min(2 ** (failures - threshold - 1), cap)
                await awaited(r.set(lock_key, "1", ex=backoff))

    async def clear(self, target: PairingTarget, source_key: str) -> None:
        """Reset a source's failure counter and lock after a VALID redeem, so an honest user
        who fat-fingered a code first is not left throttled."""
        fail_key = self.settings.redeem_fail_key(target.target_kind, target.target_name, source_key)
        lock_key = self.settings.redeem_lock_key(target.target_kind, target.target_name, source_key)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.delete(fail_key))
            await awaited(r.delete(lock_key))


__all__ = ["ConversationRedeemThrottle"]
