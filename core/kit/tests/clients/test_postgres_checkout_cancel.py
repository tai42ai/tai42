"""A cancellation requested while a pooled checkout is in flight reaches the task that asked for the connection.

The pool's checkout validates a connection with the ``check`` callback; a ``CancelledError`` that lands
while the check runs is caught by the pool, which discards that attempt and hands out another
connection. The kit's pool re-raises such a cancellation once the connection is back in the pool, on
both checkout doors (``getconn`` and ``connection()``). A task whose cancellation was delivered and
handled before the checkout still gets its connection.
Hermetic: the pool is the kit's real pool built by ``_open_named_pool``, over a fake driver connection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from tai42_kit.clients.impl import postgres as pg_mod

_DSN = "postgresql://u@h/db"


class _FakePgconn:
    transaction_status = 0  # psycopg.pq.TransactionStatus.IDLE


class _FakeConn:
    """A driver connection whose ``execute`` parks while the test holds the gate open."""

    gate: asyncio.Event | None = None
    in_check: asyncio.Event | None = None
    fail_after_park = False

    def __init__(self) -> None:
        self.pgconn = _FakePgconn()
        self.closed = False
        self.broken = False
        self.autocommit = False

    async def set_autocommit(self, value: bool) -> None:
        self.autocommit = value

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        gate, in_check = _FakeConn.gate, _FakeConn.in_check
        if gate is not None and in_check is not None and not in_check.is_set():
            in_check.set()
            await gate.wait()
        elif _FakeConn.fail_after_park:
            raise RuntimeError("the server is unreachable")

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
async def pool(monkeypatch: pytest.MonkeyPatch):
    async def _fake_connect(conninfo: str, **kwargs: Any) -> _FakeConn:
        return _FakeConn()

    monkeypatch.setattr(pg_mod.AsyncConnection, "connect", _fake_connect)
    monkeypatch.setattr(_FakeConn, "gate", asyncio.Event())
    monkeypatch.setattr(_FakeConn, "in_check", asyncio.Event())
    built = await pg_mod._open_named_pool(_DSN, min_size=1, max_size=1, name="checkout-cancel", retry_attempts=0)
    try:
        yield built
    finally:
        await built.close()


async def _cancel_inside_the_check(task: asyncio.Task[Any]) -> None:
    """Wait until ``task`` is parked inside the checkout's check, then cancel it."""
    assert _FakeConn.in_check is not None
    await asyncio.wait_for(_FakeConn.in_check.wait(), timeout=5)
    task.cancel()


async def test_a_cancel_during_getconn_is_reraised_and_the_connection_returned(pool) -> None:
    handed_out: list[object] = []

    async def checkout() -> None:
        handed_out.append(await pool.getconn(timeout=10))

    task = asyncio.create_task(checkout())
    await _cancel_inside_the_check(task)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert task.cancelled()
    assert handed_out == []
    assert pool.get_stats()["pool_available"] == 1


async def test_a_cancel_during_connection_is_reraised_and_the_body_never_runs(pool) -> None:
    body_ran: list[bool] = []

    async def checkout() -> None:
        async with pool.connection(timeout=10):
            body_ran.append(True)

    task = asyncio.create_task(checkout())
    await _cancel_inside_the_check(task)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert task.cancelled()
    assert body_ran == []
    assert pool.get_stats()["pool_available"] == 1


async def test_a_cancel_during_a_checkout_that_then_times_out_is_reraised(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_FakeConn, "fail_after_park", True)  # every check after the parked one fails

    async def checkout() -> None:
        await pool.getconn(timeout=0.5)

    task = asyncio.create_task(checkout())
    await _cancel_inside_the_check(task)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert task.cancelled()


async def test_a_cancel_handled_before_the_checkout_still_gets_a_connection(pool) -> None:
    assert _FakeConn.in_check is not None
    _FakeConn.in_check.set()  # no parking: the check passes at once
    started = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def checkout() -> None:
        try:
            started.set()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            assert current is not None
            outcome["cancelling_before"] = current.cancelling()
        async with pool.connection(timeout=10) as conn:
            outcome["conn"] = conn

    task = asyncio.create_task(checkout())
    await started.wait()
    task.cancel()
    await asyncio.wait_for(task, timeout=10)

    assert outcome["cancelling_before"] == 1
    assert isinstance(outcome["conn"], _FakeConn)
    assert pool.get_stats()["pool_available"] == 1
