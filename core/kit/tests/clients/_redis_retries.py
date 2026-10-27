"""Zero-backoff kit logged retries and a ``do`` that fails a set number of times."""

from __future__ import annotations

from typing import Any

from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from tai42_kit.clients.impl import redis as redis_mod


def async_retry(retries: int = 2, label: str = "L") -> Any:
    """A kit async logged retry over ``retries`` retries with no backoff delay."""
    return redis_mod._LoggedAsyncRetry(ExponentialBackoff(cap=0, base=0), retries, label=label)


def sync_retry(retries: int = 2, label: str = "L") -> Any:
    """A kit sync logged retry over ``retries`` retries with no backoff delay."""
    return redis_mod._LoggedSyncRetry(ExponentialBackoff(cap=0, base=0), retries, label=label)


class Flaky:
    """A ``do`` that raises ``ConnectionError`` ``times`` times, then returns ``"ok"``."""

    def __init__(self, times: int) -> None:
        self.times = times
        self.calls = 0
        self.errors: list[BaseException] = []

    def step(self) -> str:
        self.calls += 1
        if self.calls <= self.times:
            error = RedisConnectionError(f"Error 111 connecting to h:1 #{self.calls}")
            self.errors.append(error)
            raise error
        return "ok"

    async def __call__(self) -> str:
        return self.step()
