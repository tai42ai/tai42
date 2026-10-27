"""A kit connection's retry view.

A kit connection's ``retry`` is a view of the retry it was given: a call made from inside
a retried operation's ``do`` runs once under the outer budget, a call made from ``fail``
gets a budget of its own, and the view mirrors and forwards the given retry's settings.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

pytest.importorskip("redis")

from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from tai42_kit.clients.impl import redis as redis_mod
from tai42_kit.clients.impl import redis_connections as connections_mod

from ._redis_kit_connections import UNDIALLED, async_kit_connection, sync_kit_connection
from ._redis_log import LOGGER, kit_warnings
from ._redis_retries import Flaky, async_retry, sync_retry


def _async_view(retry: Any = None) -> Any:
    """The retry view of a kit async connection over ``retry`` (the zero-backoff kit retry by default)."""
    return async_kit_connection(UNDIALLED, retry=retry or async_retry()).retry


def _sync_view(retry: Any = None) -> Any:
    """The retry view of a kit sync connection over ``retry`` (the zero-backoff kit retry by default)."""
    return sync_kit_connection(UNDIALLED, retry=retry or sync_retry()).retry


async def test_async_a_call_from_inside_do_runs_once_under_the_outer_budget(caplog: pytest.LogCaptureFixture) -> None:
    retry = _async_view()
    assert isinstance(retry, connections_mod._AsyncConnectionRetry)
    inner_calls = 0
    inner_fails = 0

    async def inner_do() -> None:
        nonlocal inner_calls
        inner_calls += 1
        raise ConnectionRefusedError("refused")

    async def inner_fail(error: BaseException) -> None:
        nonlocal inner_fails
        inner_fails += 1

    async def outer_do() -> None:
        try:
            await retry.call_with_retry(inner_do, inner_fail)
        except OSError as exc:
            raise RedisConnectionError(str(exc)) from exc

    async def outer_fail(error: BaseException) -> None:
        pass

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        await retry.call_with_retry(outer_do, outer_fail)
    assert inner_calls == 3
    assert inner_fails == 0
    assert len(kit_warnings(caplog)) == 2


def test_sync_a_call_from_inside_do_runs_once_under_the_outer_budget(caplog: pytest.LogCaptureFixture) -> None:
    retry = _sync_view()
    assert isinstance(retry, connections_mod._SyncConnectionRetry)
    calls = {"inner": 0, "inner_fail": 0}

    def inner_do() -> None:
        calls["inner"] += 1
        raise ConnectionRefusedError("refused")

    def inner_fail(error: BaseException) -> None:
        calls["inner_fail"] += 1

    def outer_do() -> None:
        try:
            retry.call_with_retry(inner_do, inner_fail)
        except OSError as exc:
            raise RedisConnectionError(str(exc)) from exc

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        retry.call_with_retry(outer_do, lambda error: None)
    assert calls == {"inner": 3, "inner_fail": 0}
    assert len(kit_warnings(caplog)) == 2


async def test_async_a_call_from_fail_has_a_budget_of_its_own(caplog: pytest.LogCaptureFixture) -> None:
    retry = _async_view()
    retry.update_supported_errors([OSError])
    inner_calls = 0

    async def inner_do() -> None:
        nonlocal inner_calls
        inner_calls += 1
        raise ConnectionRefusedError("refused")

    async def inner_fail(error: BaseException) -> None:
        pass

    async def outer_do() -> None:
        raise RedisConnectionError("read lost")

    async def outer_fail(error: BaseException) -> None:
        await retry.call_with_retry(inner_do, inner_fail)

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(ConnectionRefusedError):
        await retry.call_with_retry(outer_do, outer_fail)
    assert inner_calls == 3
    assert len(kit_warnings(caplog)) == 2


def test_sync_a_call_from_fail_has_a_budget_of_its_own(caplog: pytest.LogCaptureFixture) -> None:
    retry = _sync_view()
    retry.update_supported_errors([OSError])
    calls = 0

    def inner_do() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError("refused")

    def outer_do() -> None:
        raise RedisConnectionError("read lost")

    def outer_fail(error: BaseException) -> None:
        retry.call_with_retry(inner_do, lambda error: None)

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(ConnectionRefusedError):
        retry.call_with_retry(outer_do, outer_fail)
    assert calls == 3
    assert len(kit_warnings(caplog)) == 2


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_the_view_forwards_the_supported_errors_and_mirrors_the_given_retry(sync: bool) -> None:
    given = sync_retry(retries=4) if sync else async_retry(retries=4)
    connection = (sync_kit_connection if sync else async_kit_connection)(UNDIALLED, retry=given)
    view = connection.retry
    # The connection holds the copy redis-py made of the retry it was given.
    copied = view._given
    assert type(copied) is type(given)
    view.update_supported_errors([OSError])
    assert OSError in copied._supported_errors
    assert view._supported_errors == copied._supported_errors
    assert (view._backoff, view.get_retries()) == (copied._backoff, 4)
    view.update_retries(1)
    assert (view.get_retries(), copied._retries) == (1, 1)


async def test_async_the_view_is_outermost_again_after_a_raise_and_after_a_cancel_in_the_backoff() -> None:
    retry = _async_view()

    async def fail(error: BaseException) -> None:
        pass

    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(Flaky(10), fail)
    assert retry._depth == 0
    again = Flaky(10)
    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(again, fail)
    assert again.calls == 3

    # A long backoff, so the call is parked in its sleep when it is cancelled.
    slow = _async_view(redis_mod._LoggedAsyncRetry(ExponentialBackoff(cap=60, base=60), 2, label="L"))
    sleeping = Flaky(10)
    task = asyncio.create_task(slow.call_with_retry(sleeping, fail))
    while sleeping.calls == 0:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleeping.calls == 1
    assert slow._depth == 0


def test_sync_the_view_is_outermost_again_after_a_raise() -> None:
    retry = _sync_view()
    with pytest.raises(RedisConnectionError):
        retry.call_with_retry(Flaky(10).step, lambda error: None)
    assert retry._depth == 0
    again = Flaky(10)
    with pytest.raises(RedisConnectionError):
        retry.call_with_retry(again.step, lambda error: None)
    assert again.calls == 3
