"""The finished-thread ledger: one contract on every provider, and the mark/unmark calls every owner uses."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm.checkpoint import ThreadRetention
from tai42_kit.llm.checkpoint import ledger as ledger_mod
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.checkpoint.ledger import (
    REDIS_FINISHED_THREADS_KEY,
    MemoryFinishedThreadLedger,
    RedisFinishedThreadLedger,
    SqliteFinishedThreadLedger,
    mark_threads_active,
    mark_threads_finished,
)
from tai42_kit.settings import reset_all_settings

from ._real_redis import real_redis_url

_T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


async def _memory_ledger(_tmp: Path) -> AsyncIterator[Any]:
    yield MemoryFinishedThreadLedger()


async def _sqlite_ledger(tmp: Path) -> AsyncIterator[Any]:
    aiosqlite = pytest.importorskip("aiosqlite")
    conn = await aiosqlite.connect(str(tmp / "ledger.db"))
    try:
        ledger = SqliteFinishedThreadLedger(conn)
        await ledger.setup()
        await ledger.setup()  # idempotent
        yield ledger
    finally:
        await conn.close()


async def _redis_ledger(_tmp: Path) -> AsyncIterator[Any]:
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeAsyncRedis()
    try:
        yield RedisFinishedThreadLedger(client)
    finally:
        await client.aclose()


async def _redis_server_ledger(_tmp: Path) -> AsyncIterator[Any]:
    from redis.asyncio import Redis

    client = Redis.from_url(real_redis_url())
    try:
        await client.delete(REDIS_FINISHED_THREADS_KEY)
        yield RedisFinishedThreadLedger(client)
    finally:
        await client.delete(REDIS_FINISHED_THREADS_KEY)
        await client.aclose()


async def _postgres_ledger(_tmp: Path) -> AsyncIterator[Any]:
    if os.environ.get("TAI42_KIT_REAL_PG") not in ("1", "true", "True"):
        pytest.skip(
            "real-Postgres ledger test is opt-in: set TAI42_KIT_REAL_PG=1 and the TAI_DATABASE_DEFAULT_PG_* env"
        )
    from tai42_kit.clients.impl.postgres import open_dedicated_pool
    from tai42_kit.db import database_settings
    from tai42_kit.llm._langgraph_postgres import LANGGRAPH_CONNECTION_KWARGS
    from tai42_kit.llm.checkpoint import postgres_store
    from tai42_kit.llm.checkpoint.ledger import PostgresFinishedThreadLedger

    pool = await open_dedicated_pool(
        database_settings("default").pg_dsn, role="test", connection_kwargs=dict(LANGGRAPH_CONNECTION_KWARGS)
    )
    try:
        async with pool.connection() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {postgres_store.FINISHED_TABLE}")
            await conn.execute(f"DROP TABLE IF EXISTS {postgres_store.RETENTION_TABLE}")
        ledger = PostgresFinishedThreadLedger(pool)
        await ledger.setup()
        await ledger.setup()  # idempotent
        yield ledger
    finally:
        await pool.close()


_FACTORIES: dict[str, Callable[[Path], AsyncIterator[Any]]] = {
    "memory": _memory_ledger,
    "sqlite": _sqlite_ledger,
    "redis": _redis_ledger,
    "redis-server": pytest.param(_redis_server_ledger, marks=pytest.mark.integration),  # type: ignore[dict-item]
    "postgres": pytest.param(_postgres_ledger, marks=pytest.mark.integration),  # type: ignore[dict-item]
}


@pytest.fixture(params=list(_FACTORIES.values()), ids=list(_FACTORIES))
async def ledger(request, tmp_path) -> AsyncIterator[Any]:
    factory = request.param
    gen = factory(tmp_path)
    value = await anext(gen)
    try:
        yield value
    finally:
        await gen.aclose()


async def test_marked_threads_are_finished_before_a_later_cutoff_oldest_first(ledger):
    await ledger.mark(["b"], _T0 + timedelta(minutes=2))
    await ledger.mark(["a", "c"], _T0)
    assert await ledger.finished_before(_T0 + timedelta(minutes=5), limit=10) == ["a", "c", "b"]
    assert await ledger.finished_before(_T0 + timedelta(minutes=1), limit=10) == ["a", "c"]
    assert await ledger.finished_before(_T0 - timedelta(seconds=1), limit=10) == []


async def test_the_cutoff_is_inclusive(ledger):
    await ledger.mark(["a"], _T0)
    assert await ledger.finished_before(_T0, limit=10) == ["a"]


async def test_limit_pages_the_result(ledger):
    await ledger.mark([f"t{i}" for i in range(5)], _T0)
    page = await ledger.finished_before(_T0, limit=2)
    assert len(page) == 2


async def test_a_later_mark_moves_the_time(ledger):
    await ledger.mark(["a"], _T0)
    await ledger.mark(["a"], _T0 + timedelta(hours=1))
    assert await ledger.finished_before(_T0 + timedelta(minutes=30), limit=10) == []
    assert await ledger.finished_before(_T0 + timedelta(hours=1), limit=10) == ["a"]


async def test_forget_removes_a_mark(ledger):
    await ledger.mark(["a", "b"], _T0)
    await ledger.forget(["a", "unknown"])
    assert await ledger.finished_before(_T0, limit=10) == ["b"]


async def test_an_empty_sequence_changes_nothing(ledger):
    await ledger.mark(["a"], _T0)
    await ledger.mark([], _T0 + timedelta(hours=1))
    await ledger.forget([])
    assert await ledger.finished_before(_T0, limit=10) == ["a"]


async def test_a_naive_time_is_refused(ledger):
    with pytest.raises(ValueError, match="timezone-aware"):
        await ledger.mark(["a"], datetime(2026, 1, 1))


# A store whose waiting horizon is swept records the owner's declared waiting at ``start``.
_RECORDS_WAITING = {"memory", "sqlite", "postgres"}


def _provider_of(request) -> str:
    return request.node.callspec.id


async def test_start_removes_the_finished_mark(ledger):
    await ledger.mark(["a", "b"], _T0)
    await ledger.start(["a"], ThreadRetention(30, 10))
    await ledger.start(["b"], None)
    assert await ledger.finished_before(_T0, limit=10) == []


async def test_start_records_the_declared_waiting_where_the_store_sweeps_it(ledger, request):
    await ledger.start(["a", "b"], ThreadRetention(30, 10))
    await ledger.start(["c"], None)
    expected = {"a": 30, "b": 30} if _provider_of(request) in _RECORDS_WAITING else {}
    assert await ledger.declared_waiting(["a", "b", "c", "unknown"]) == expected


async def test_a_later_start_replaces_the_declaration_and_none_removes_it(ledger, request):
    await ledger.start(["a", "b"], ThreadRetention(30, 10))
    await ledger.start(["a"], ThreadRetention(45, 10))
    await ledger.start(["b"], None)
    expected = {"a": 45} if _provider_of(request) in _RECORDS_WAITING else {}
    assert await ledger.declared_waiting(["a", "b"]) == expected


async def test_forget_removes_the_declaration_too(ledger):
    await ledger.start(["a", "b"], ThreadRetention(30, 10))
    await ledger.forget(["a", "b"])
    assert await ledger.declared_waiting(["a", "b"]) == {}


async def test_an_empty_sequence_starts_nothing(ledger):
    await ledger.mark(["a"], _T0)
    await ledger.start([], ThreadRetention(30, 10))
    assert await ledger.declared_waiting([]) == {}
    assert await ledger.finished_before(_T0, limit=10) == ["a"]


async def test_redis_mark_rearms_the_set_expiry_to_the_waiting_retention(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeAsyncRedis()
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "60")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", "30")
    reset_all_settings()
    try:
        ledger = RedisFinishedThreadLedger(client)
        await ledger.mark(["a"], _T0)
        await client.expire(REDIS_FINISHED_THREADS_KEY, 5)
        await ledger.mark(["b"], _T0)
        assert 3590 < await client.ttl(REDIS_FINISHED_THREADS_KEY) <= 3600
    finally:
        reset_all_settings()
        await client.aclose()


# --------------------------------------------------------------------------- #
# mark_threads_finished / mark_threads_active — through the registry's ledger
# --------------------------------------------------------------------------- #
def _past_due() -> datetime:
    # Past the due time of a mark made now under the platform's default finished retention (1 day).
    return datetime.now(UTC) + timedelta(days=2)


async def _sqlite_conn(tmp_path: Path) -> str:
    pytest.importorskip("aiosqlite")
    return str(tmp_path / f"{uuid.uuid4().hex}.db")


async def test_marks_land_in_the_deployment_store_by_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "memory")
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        await mark_threads_finished(["t1", "t2"])
        ledger = await registry.ledger("memory", None)
        assert sorted(await ledger.finished_before(_past_due(), limit=10)) == ["t1", "t2"]
        await mark_threads_active(["t1"])
        assert await ledger.finished_before(_past_due(), limit=10) == ["t2"]
    finally:
        await registry.close_all()
        reset_all_settings()


async def test_a_per_run_provider_reaches_that_store_ledger_with_the_default_conn_string(monkeypatch, tmp_path):
    db = await _sqlite_conn(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "memory")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_CONN_STRING", db)
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        await mark_threads_finished(["run-thread"], provider="sqlite")
        sqlite_ledger = await registry.ledger("sqlite", db)
        memory_ledger = await registry.ledger("memory", db)
        assert await sqlite_ledger.finished_before(_past_due(), limit=10) == ["run-thread"]
        assert await memory_ledger.finished_before(_past_due(), limit=10) == []
        await mark_threads_active(["run-thread"], provider="sqlite")
        assert await sqlite_ledger.finished_before(_past_due(), limit=10) == []
    finally:
        await registry.close_all()
        reset_all_settings()


async def test_an_explicit_conn_string_is_used(monkeypatch, tmp_path):
    db = await _sqlite_conn(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "memory")
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        await mark_threads_finished(["x"], provider="sqlite", conn_string=db)
        assert await (await registry.ledger("sqlite", db)).finished_before(_past_due(), limit=10) == ["x"]
    finally:
        await registry.close_all()
        reset_all_settings()


async def test_an_empty_sequence_builds_no_store(monkeypatch):
    built: list[Any] = []

    async def _no_ledger(*args: Any) -> Any:
        built.append(args)
        raise AssertionError("no store is built for an empty sequence")

    monkeypatch.setattr(ledger_mod, "_ledger", _no_ledger)
    await mark_threads_finished([])
    await mark_threads_active([])
    assert built == []


async def test_a_store_error_propagates(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "sqlite")
    reset_all_settings()
    try:
        with pytest.raises(ValueError, match="sqlite checkpoint provider requires a conn_string"):
            await mark_threads_finished(["t"])
    finally:
        reset_all_settings()


async def test_a_finished_mark_is_due_the_declared_finished_life_after_the_terminal(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "memory")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "600")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", "120")
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        before = datetime.now(UTC)
        await mark_threads_finished(["declared"], retention=ThreadRetention(10, 3))
        await mark_threads_finished(["platform"])
        ledger = await registry.ledger("memory", None)
        assert await ledger.finished_before(before + timedelta(minutes=2), limit=10) == []
        assert await ledger.finished_before(before + timedelta(minutes=4), limit=10) == ["declared"]
        assert await ledger.finished_before(before + timedelta(minutes=119), limit=10) == ["declared"]
        assert await ledger.finished_before(before + timedelta(minutes=121), limit=10) == ["declared", "platform"]
    finally:
        await registry.close_all()
        reset_all_settings()


async def test_marking_active_with_a_retention_records_it_and_removes_the_mark(monkeypatch, tmp_path):
    db = await _sqlite_conn(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "sqlite")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_CONN_STRING", db)
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        await mark_threads_finished(["t"])
        await mark_threads_active(["t"], retention=ThreadRetention(5, 1))
        ledger = await registry.ledger("sqlite", db)
        assert await ledger.finished_before(datetime.now(UTC) + timedelta(days=2), limit=10) == []
        assert await ledger.declared_waiting(["t"]) == {"t": 5}
        await mark_threads_active(["t"])
        assert await ledger.declared_waiting(["t"]) == {}
    finally:
        await registry.close_all()
        reset_all_settings()
