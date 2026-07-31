"""Login rate limiting — failures-only, Redis-backed, fail-closed.

Only a FAILED attempt records against the counters, so a correct login is never
blocked and an attacker cannot lock a victim out. Two dimensions:

- per-account: a consecutive-failure counter; past the backoff threshold each
  further failure is locked with capped exponential backoff. A success clears it.
- per-IP: a fixed-window failure count across all emails from one source.

Every key carries the per-deployment ``redis_key_prefix`` namespace. A Redis
failure propagates (fail closed). Client IP is the direct peer — no
``X-Forwarded-For`` parsing; proxied deployments throttle at their ingress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from tai42_kit.clients import RedisConnectionSettings, client_ctx
from tai42_kit.clients.impl.redis import RedisClient

if TYPE_CHECKING:
    from tai42_accounts_postgres.settings import AccountsSettings


class RateLimitedError(Exception):
    """Login throttled. ``retry_after`` is whole seconds until the next attempt."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"login throttled; retry after {retry_after}s")
        self.retry_after = retry_after


class RateLimiter:
    """Failures-only login throttle over the injected Redis."""

    def __init__(self, redis_settings: Any, settings: AccountsSettings) -> None:
        self._redis_settings = redis_settings
        self._settings = settings

    def _redis(self) -> RedisConnectionSettings:
        # Bridge the contract's ``Any`` redis to kit's nominal settings type.
        return cast("RedisConnectionSettings", self._redis_settings)

    def _account_key(self, email: str) -> str:
        return f"{self._settings.key_prefix}:acc:login:fail:{email}"

    def _ip_key(self, ip: str) -> str:
        return f"{self._settings.key_prefix}:acc:login:ip:{ip}"

    async def record_failure(self, email: str | None, ip: str) -> None:
        """Count a failed attempt and raise :class:`RateLimitedError` if over-limit.

        The per-IP dimension always applies; the per-account dimension applies only
        when ``email`` is given (password login). The token-gated routes (bootstrap,
        invite accept) have no account and pass ``email=None``, throttling per IP.

        A Redis failure propagates (fail closed).
        """
        threshold = self._settings.login_backoff_threshold
        cap = self._settings.login_backoff_cap_seconds
        ip_max = self._settings.login_ip_max_attempts
        ip_window = self._settings.login_ip_window_seconds

        ip_key = self._ip_key(ip)

        async with client_ctx(RedisClient, self._redis()) as r:
            retry_after = 0

            if email is not None:
                account_key = self._account_key(email)
                account_failures = await r.incr(account_key)
                # Counter decays after ``cap`` of inactivity so a dormant account
                # is not permanently escalated.
                await r.expire(account_key, cap)
                if account_failures > threshold:
                    # First lock lands the attempt after the threshold is crossed:
                    # exponent starts at zero (1 s), doubling each failure, capped.
                    retry_after = min(2 ** (account_failures - threshold - 1), cap)

            ip_failures = await r.incr(ip_key)
            if ip_failures == 1:
                # First failure in a fresh window opens the fixed window.
                await r.expire(ip_key, ip_window)
            if ip_failures > ip_max:
                ip_ttl = await r.ttl(ip_key)
                # Remaining window, or the full window if Redis reports no expiry.
                ip_retry = ip_ttl if ip_ttl and ip_ttl > 0 else ip_window
                retry_after = max(retry_after, ip_retry)

        if retry_after > 0:
            raise RateLimitedError(retry_after)

    async def clear(self, email: str) -> None:
        """Reset the per-account failure counter after a successful login.

        A Redis failure propagates (fail closed).
        """
        async with client_ctx(RedisClient, self._redis()) as r:
            await r.delete(self._account_key(email))
