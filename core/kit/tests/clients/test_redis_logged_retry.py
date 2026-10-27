"""The kit's logged retry, driven directly.

A redis-py ``Retry`` subclass that logs one WARNING per retried failure, named by a
setting and never carrying the URL's credentials, and that passes the loop's extra
arguments through on every loop shape redis-py has.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("redis")

import redis.asyncio.retry as async_retry_mod
import redis.retry as sync_retry_mod
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from tai42_kit.clients.impl import redis as redis_mod

from ._redis_log import LOGGER, MARKER_PASSWORD, kit_warnings
from ._redis_retries import Flaky, async_retry, sync_retry


async def test_async_failures_within_the_budget_log_one_warning_each(caplog: pytest.LogCaptureFixture) -> None:
    do = Flaky(2)
    failed: list[tuple[Any, ...]] = []

    async def fail(*args: Any) -> None:
        failed.append(args)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await async_retry().call_with_retry(do, fail) == "ok"
    assert failed == [(do.errors[0],), (do.errors[1],)]
    assert kit_warnings(caplog) == [
        f"Redis connection L failed (attempt 1 of 3): {do.errors[0]}; retrying in 0.000s",
        f"Redis connection L failed (attempt 2 of 3): {do.errors[1]}; retrying in 0.000s",
    ]


def test_sync_failures_within_the_budget_log_one_warning_each(caplog: pytest.LogCaptureFixture) -> None:
    flaky = Flaky(2)
    failed: list[tuple[Any, ...]] = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert sync_retry().call_with_retry(flaky.step, lambda *args: failed.append(args)) == "ok"
    assert failed == [(flaky.errors[0],), (flaky.errors[1],)]
    assert len(kit_warnings(caplog)) == 2


async def test_async_an_exhausted_budget_logs_n_warnings_and_raises_the_last_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    do = Flaky(10)

    async def fail(error: BaseException) -> None:
        pass

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as excinfo:
        await async_retry().call_with_retry(do, fail)
    assert excinfo.value is do.errors[-1]
    assert do.calls == 3
    assert len(kit_warnings(caplog)) == 2


def test_sync_an_exhausted_budget_logs_n_warnings_and_raises_the_last_error(caplog: pytest.LogCaptureFixture) -> None:
    flaky = Flaky(10)
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as excinfo:
        sync_retry().call_with_retry(flaky.step, lambda error: None)
    assert excinfo.value is flaky.errors[-1]
    assert flaky.calls == 3
    assert len(kit_warnings(caplog)) == 2


def test_the_logged_delay_is_the_next_backoff(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_retry_mod, "sleep", lambda delay: None)
    retry = redis_mod.redis_from_url_kwargs({"url": "redis://h", "retry_attempts": 2}, sync=True, label="L")["retry"]
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(Flaky(10).step, lambda error: None)
    assert [line.rsplit(" ", 1)[1] for line in kit_warnings(caplog)] == ["0.016s", "0.032s"]


def test_a_url_password_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    url = f"redis://user:{MARKER_PASSWORD}@127.0.0.1:1/0"
    retry = redis_mod.redis_from_url_kwargs({"url": url, "retry_attempts": 1}, sync=True)["retry"]
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(Flaky(10).step, lambda error: None)
    assert kit_warnings(caplog)
    assert MARKER_PASSWORD not in caplog.text


@pytest.mark.parametrize("make", [async_retry, sync_retry], ids=["async", "sync"])
def test_a_deep_copy_keeps_the_subclass_label_and_budget(make: Callable[..., Any]) -> None:
    retry = make(retries=4, label="COPY")
    copied = copy.deepcopy(retry)
    assert type(copied) is type(retry)
    assert (copied._label, copied._budget, copied._retries) == ("COPY", 4, 4)


# The 7.x loop: ``call_with_retry(do, fail, is_retryable=None, with_failure_count=False)``,
# with ``fail(error, failures)`` when the count is asked for (redis 7.4.1 ``redis/retry.py``).
class _AsyncSevenLoop(async_retry_mod.Retry):
    __slots__ = ()

    async def call_with_retry(  # type: ignore[override]
        self, do: Any, fail: Any, is_retryable: Any = None, with_failure_count: bool = False
    ) -> Any:
        self._backoff.reset()
        failures = 0
        while True:
            try:
                return await do()
            except self._supported_errors as error:
                if is_retryable and not is_retryable(error):
                    raise
                failures += 1
                if with_failure_count:
                    await fail(error, failures)
                else:
                    await fail(error)
                if self._retries >= 0 and failures > self._retries:
                    raise error
                await asyncio.sleep(self._backoff.compute(failures))


class _SyncSevenLoop(sync_retry_mod.Retry):
    __slots__ = ()

    def call_with_retry(  # type: ignore[override]
        self, do: Any, fail: Any, is_retryable: Any = None, with_failure_count: bool = False
    ) -> Any:
        self._backoff.reset()
        failures = 0
        while True:
            try:
                return do()
            except self._supported_errors as error:
                if is_retryable and not is_retryable(error):
                    raise
                failures += 1
                if with_failure_count:
                    fail(error, failures)
                else:
                    fail(error)
                if self._retries >= 0 and failures > self._retries:
                    raise error


class _AsyncOverSeven(redis_mod._LoggedAsyncRetry, _AsyncSevenLoop):
    __slots__ = ()


class _SyncOverSeven(redis_mod._LoggedSyncRetry, _SyncSevenLoop):
    __slots__ = ()


async def test_async_the_seven_loop_passes_the_failure_count_through(caplog: pytest.LogCaptureFixture) -> None:
    retry = _AsyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    do = Flaky(2)
    failed: list[tuple[Any, ...]] = []

    async def fail(error: BaseException, count: int) -> None:
        failed.append((error, count))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await retry.call_with_retry(do, fail, with_failure_count=True) == "ok"
    assert failed == [(do.errors[0], 1), (do.errors[1], 2)]
    assert len(kit_warnings(caplog)) == 2


def test_sync_the_seven_loop_passes_the_failure_count_through(caplog: pytest.LogCaptureFixture) -> None:
    retry = _SyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    flaky = Flaky(2)
    failed: list[tuple[Any, ...]] = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert retry.call_with_retry(flaky.step, lambda e, n: failed.append((e, n)), with_failure_count=True) == "ok"
    assert failed == [(flaky.errors[0], 1), (flaky.errors[1], 2)]
    assert len(kit_warnings(caplog)) == 2


async def test_async_a_seven_loop_rejection_raises_at_once_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    retry = _AsyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    do = Flaky(1)
    failed: list[object] = []

    async def fail(*args: Any) -> None:
        failed.append(args)

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as excinfo:
        await retry.call_with_retry(do, fail, is_retryable=lambda e: False)
    assert excinfo.value is do.errors[0]
    assert failed == []
    assert kit_warnings(caplog) == []


def test_sync_a_seven_loop_rejection_raises_at_once_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    retry = _SyncOverSeven(ExponentialBackoff(cap=0, base=0), 3, label="L")
    flaky = Flaky(1)
    failed: list[object] = []
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(flaky.step, failed.append, is_retryable=lambda e: False)
    assert failed == []
    assert kit_warnings(caplog) == []


async def test_async_the_override_forwards_the_callers_extra_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def spy(self: Any, do: Any, fail: Any, *args: Any, **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "ok"

    monkeypatch.setattr(async_retry_mod.Retry, "call_with_retry", spy)

    async def fail(error: BaseException) -> None:
        pass

    assert await async_retry().call_with_retry(Flaky(0), fail, "x", flag=True) == "ok"
    assert seen == [(("x",), {"flag": True})]


def test_sync_the_override_forwards_the_callers_extra_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def spy(self: Any, do: Any, fail: Any, *args: Any, **kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "ok"

    monkeypatch.setattr(sync_retry_mod.Retry, "call_with_retry", spy)
    assert sync_retry().call_with_retry(Flaky(0).step, lambda error: None, "x", flag=True) == "ok"
    assert seen == [(("x",), {"flag": True})]
