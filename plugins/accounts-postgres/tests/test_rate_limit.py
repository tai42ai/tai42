"""Failures-only login throttling, fail-closed on a Redis outage."""

from __future__ import annotations

import pytest

from tai42_accounts_postgres import rate_limit
from tai42_accounts_postgres.rate_limit import RateLimitedError, RateLimiter
from tai42_accounts_postgres.settings import AccountsSettings

from .conftest import FakeRedis, make_redis_ctx


def _limiter(monkeypatch, fake: FakeRedis) -> RateLimiter:
    monkeypatch.setattr(rate_limit, "client_ctx", make_redis_ctx(fake))
    return RateLimiter(object(), AccountsSettings())


async def test_account_failures_up_to_threshold_do_not_raise(monkeypatch):
    fake = FakeRedis()
    limiter = _limiter(monkeypatch, fake)
    # Default threshold is 5, a free allowance: the first FIVE failures pass; the
    # lock lands only on the sixth (the first attempt after the threshold crossed).
    for _ in range(5):
        await limiter.record_failure("a@b.c", "10.0.0.1")


async def test_account_failure_after_threshold_raises_with_retry_after(monkeypatch):
    fake = FakeRedis()
    limiter = _limiter(monkeypatch, fake)
    for _ in range(5):
        await limiter.record_failure("a@b.c", "10.0.0.1")
    with pytest.raises(RateLimitedError) as exc:
        # The sixth failure is the first to lock, with a 1 s (2**0) backoff.
        await limiter.record_failure("a@b.c", "10.0.0.1")
    assert exc.value.retry_after == 1


async def test_clear_resets_account_counter(monkeypatch):
    fake = FakeRedis()
    limiter = _limiter(monkeypatch, fake)
    for _ in range(4):
        await limiter.record_failure("a@b.c", "10.0.0.1")
    await limiter.clear("a@b.c")
    # After a clear the counter starts over — four more failures still pass.
    for _ in range(4):
        await limiter.record_failure("a@b.c", "10.0.0.2")


async def test_ip_dimension_trips_without_an_account(monkeypatch):
    fake = FakeRedis()
    limiter = _limiter(monkeypatch, fake)
    # 30 per window is the default; the 31st IP failure locks, even email-less.
    for _ in range(30):
        await limiter.record_failure(None, "10.0.0.9")
    with pytest.raises(RateLimitedError):
        await limiter.record_failure(None, "10.0.0.9")


async def test_redis_down_propagates_fail_closed(monkeypatch):
    fake = FakeRedis(raise_on=RuntimeError("redis down"))
    limiter = _limiter(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="redis down"):
        await limiter.record_failure("a@b.c", "10.0.0.1")
