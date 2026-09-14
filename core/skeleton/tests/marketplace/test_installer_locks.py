"""The concurrency guard and the fleet advisory-lock internals."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from tai42_skeleton.marketplace import installer as installer_module
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.errors import (
    OperationInProgressError,
)

from ._specs import make_resolved, make_spec
from .test_installer import (
    Harness,
)

# -- concurrency -------------------------------------------------------------


async def test_operation_in_progress_refuses_second_same_worker_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    # The per-worker fast path: while the operation lock is held, a second
    # same-worker call is refused immediately by _guard, before any work.
    async with locks.operation_lock:
        assert locks.operation_lock.locked() is True
        with pytest.raises(OperationInProgressError):
            await h.installer().install("tai42/toolbox")
    assert locks.operation_lock.locked() is False


async def test_fleet_lock_held_elsewhere_touches_nothing() -> None:
    h = Harness()
    h.fleet.held = True
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(OperationInProgressError):
        await h.installer().install("tai42/toolbox")
    # No store/registry/pip/manifest call was made.
    assert h.registry.resolve_calls == []
    assert h.pip.calls == []
    assert h.store.record_calls == []
    assert h.svc.writes == []
    assert "store:get" not in h.events


# -- _fleet_lock internals ---------------------------------------------------


class _RecordingCursor:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql: str, params=None) -> None:
        self._log.append(" ".join(sql.split()))

    async def fetchone(self):
        return (True,)


class _RecordingConn:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.autocommit_before: list[str] = []

    async def set_autocommit(self, value: bool) -> None:
        # Snapshot the statements seen so far to prove autocommit precedes the try-lock.
        self._log.append(f"autocommit={value}")

    def cursor(self):
        return _RecordingCursor(self._log)


class _RecordingPool:
    def __init__(self, log: list[str], closed: list[bool]) -> None:
        self._log = log
        self._closed = closed

    @asynccontextmanager
    async def connection(self):
        yield _RecordingConn(self._log)


def _patch_fleet_client(monkeypatch: pytest.MonkeyPatch, log: list[str], closed: list[bool]):
    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        assert fresh is True  # the one-shot dedicated client
        assert kwargs.get("min_size") == 1
        assert kwargs.get("max_size") == 1
        try:
            yield _RecordingPool(log, closed)
        finally:
            closed.append(True)  # the fresh path closes on ANY exit

    monkeypatch.setattr(locks, "client_ctx", fake_ctx)

    class _Settings:
        def client_kwargs(self):
            return {}

    monkeypatch.setattr(locks, "component_store_settings", lambda component: _Settings())


async def test_fleet_lock_autocommit_before_trylock_and_unlock_in_finally(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    closed: list[bool] = []
    _patch_fleet_client(monkeypatch, log, closed)
    async with locks.fleet_lock():
        pass
    # autocommit set BEFORE the try-lock; unlock issued in the finally; client closed.
    assert log[0] == "autocommit=True"
    assert "pg_try_advisory_lock" in log[1]
    assert any("pg_advisory_unlock" in s for s in log)
    assert closed == [True]


async def test_fleet_lock_closes_on_body_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    closed: list[bool] = []
    _patch_fleet_client(monkeypatch, log, closed)
    with pytest.raises(RuntimeError, match="boom"):
        async with locks.fleet_lock():
            raise RuntimeError("boom")
    assert closed == [True]  # the lock connection never outlives the context


async def test_fleet_lock_closes_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    closed: list[bool] = []
    _patch_fleet_client(monkeypatch, log, closed)
    with pytest.raises(asyncio.CancelledError):
        async with locks.fleet_lock():
            raise asyncio.CancelledError
    assert closed == [True]


async def test_fleet_lock_refuses_when_lock_held_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    closed: list[bool] = []

    class _FalseCursor(_RecordingCursor):
        async def fetchone(self):
            return (False,)  # pg_try_advisory_lock returned false

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        class _Conn(_RecordingConn):
            def cursor(self):
                return _FalseCursor(log)

        class _Pool:
            @asynccontextmanager
            async def connection(self):
                yield _Conn(log)

        try:
            yield _Pool()
        finally:
            closed.append(True)

    monkeypatch.setattr(locks, "client_ctx", fake_ctx)

    class _Settings:
        def client_kwargs(self):
            return {}

    monkeypatch.setattr(locks, "component_store_settings", lambda component: _Settings())

    with pytest.raises(OperationInProgressError):
        async with locks.fleet_lock():
            pass
    assert closed == [True]
