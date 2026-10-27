"""An action run unretried on a kit connection.

An error raised through ``run_unretried`` is the error of the outermost retried operation
on the connection, raised to its caller as it is and never retried; outside any retried
operation it is raised as it is.
"""

from __future__ import annotations

import pytest

pytest.importorskip("redis")

from redis.exceptions import ConnectionError as RedisConnectionError

from ._redis_kit_connections import UNDIALLED, async_kit_connection, sync_kit_connection
from ._redis_retries import async_retry, sync_retry

_ACTION_FAILURE = "the action's own client gave up"


def test_sync_an_action_raising_inside_a_retried_operation_is_raised_by_it_unretried() -> None:
    connection = sync_kit_connection(UNDIALLED, retry=sync_retry())
    error = RedisConnectionError(_ACTION_FAILURE)
    failed: list[BaseException] = []

    def raiser() -> None:
        raise error

    def do() -> None:
        connection.run_unretried(raiser)

    with pytest.raises(RedisConnectionError) as raised:
        connection.retry.call_with_retry(do, failed.append)
    assert raised.value is error
    assert raised.value.__context__ is None
    assert failed == []


async def test_async_an_action_raising_inside_a_retried_operation_is_raised_by_it_unretried() -> None:
    connection = async_kit_connection(UNDIALLED, retry=async_retry())
    error = RedisConnectionError(_ACTION_FAILURE)
    failed: list[BaseException] = []

    def raiser() -> None:
        raise error

    async def do() -> None:
        connection.run_unretried(raiser)

    async def fail(error: BaseException) -> None:
        failed.append(error)

    with pytest.raises(RedisConnectionError) as raised:
        await connection.retry.call_with_retry(do, fail)
    assert raised.value is error
    assert raised.value.__context__ is None
    assert failed == []


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_run_unretried_outside_a_retried_operation_raises_the_error_as_it_is(sync: bool) -> None:
    connection = (sync_kit_connection if sync else async_kit_connection)(UNDIALLED)
    error = RedisConnectionError(_ACTION_FAILURE)

    def raiser() -> None:
        raise error

    with pytest.raises(RedisConnectionError) as raised:
        connection.run_unretried(raiser)
    assert raised.value is error
    assert raised.value.__context__ is None


@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
def test_run_unretried_runs_an_action_that_returns_once(sync: bool) -> None:
    connection = (sync_kit_connection if sync else async_kit_connection)(UNDIALLED)
    runs: list[None] = []
    connection.run_unretried(lambda: runs.append(None))
    assert runs == [None]
