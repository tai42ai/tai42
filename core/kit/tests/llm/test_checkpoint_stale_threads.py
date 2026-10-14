"""The stale-thread listing: one contract on every registered checkpoint store, answered by its own ledger.

A store whose waiting horizon is not its key TTL lists every thread whose newest checkpoint (in any
namespace) is older than its waiting retention — the declared one, else the default — before ``now``;
a store whose waiting horizon is its key TTL holds no thread past it and lists none. The in-process
and sqlite stores always run; real Redis and Postgres run when they are configured.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.base import empty_checkpoint

from tai42_kit.llm.checkpoint import CHECKPOINT_PROVIDERS, ThreadRetention, checkpoint_provider_facts
from tai42_kit.llm.checkpoint.checkpoint_registry import CheckpointRegistry

from ._real_redis import real_redis_url

_METADATA: dict[str, Any] = {"source": "input", "step": 0, "parents": {}}


def _conn_string(provider: str, tmp: Path) -> str | None:
    if provider == "sqlite":
        pytest.importorskip("aiosqlite")
        return str(tmp / "checkpoints.db")
    if provider == "redis":
        return real_redis_url()
    if provider == "postgres":
        if os.environ.get("TAI42_KIT_REAL_PG") not in ("1", "true", "True"):
            pytest.skip(
                "real-Postgres stale-thread test is opt-in: set TAI42_KIT_REAL_PG=1 and the "
                "TAI_DATABASE_DEFAULT_PG_* env"
            )
        from tai42_kit.db import database_settings

        return database_settings("default").pg_dsn
    return None


_LOCAL: frozenset[str] = frozenset({"memory", "sqlite"})


@pytest.fixture(
    params=[
        provider if provider in _LOCAL else pytest.param(provider, marks=pytest.mark.integration)
        for provider in sorted(CHECKPOINT_PROVIDERS)
    ]
)
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[tuple[str, Any, Any]]:
    """``(provider, saver, ledger)`` of one registered store, built through the checkpoint registry."""
    provider = request.param
    conn_string = _conn_string(provider, tmp_path)
    registry = CheckpointRegistry()
    try:
        saver = await registry.get_checkpointer(provider, conn_string)
        ledger = await registry.ledger(provider, conn_string)
        yield provider, saver, ledger
    finally:
        await registry.close_all()


async def _put(saver: Any, thread_id: str, ts: datetime, *, namespace: str = "") -> None:
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = ts.isoformat()
    config: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}}
    await saver.aput(config, checkpoint, _METADATA, {})


async def _cleanup(saver: Any, ledger: Any, thread_ids: Sequence[str]) -> None:
    for thread_id in thread_ids:
        await saver.adelete_thread(thread_id)
    await ledger.forget(list(thread_ids))


async def test_a_store_lists_the_threads_past_their_waiting_horizon(store: tuple[str, Any, Any]) -> None:
    provider, saver, ledger = store
    run = uuid.uuid4().hex[:10]
    old, fresh, revived, nested, declared_short, declared_long = (
        f"stale-{name}-{run}" for name in ("old", "fresh", "revived", "nested", "short", "long")
    )
    mine = {old, fresh, revived, nested, declared_short, declared_long}
    now = datetime.now(UTC)
    try:
        await _put(saver, old, now - timedelta(hours=4))
        await _put(saver, old, now - timedelta(hours=3))
        await _put(saver, fresh, now - timedelta(minutes=5))
        await _put(saver, revived, now - timedelta(hours=6))
        await _put(saver, revived, now - timedelta(minutes=1))
        await _put(saver, nested, now - timedelta(hours=3))
        await _put(saver, nested, now - timedelta(minutes=5), namespace="child")
        await _put(saver, declared_short, now - timedelta(minutes=5))
        await _put(saver, declared_long, now - timedelta(minutes=50))
        await ledger.start([declared_short], ThreadRetention(waiting_minutes=2, finished_minutes=1))
        await ledger.start([declared_long], ThreadRetention(waiting_minutes=55, finished_minutes=1))

        expected = (
            [] if checkpoint_provider_facts(provider).retention == "native_ttl" else sorted([old, declared_short])
        )
        for default_minutes in (60, 30):
            listed = await ledger.stale_threads(saver, now=now, default_minutes=default_minutes)
            assert listed == sorted(listed)
            assert [thread_id for thread_id in listed if thread_id in mine] == expected
    finally:
        await _cleanup(saver, ledger, sorted(mine))


async def test_a_naive_now_is_refused(store: tuple[str, Any, Any]) -> None:
    _provider, saver, ledger = store
    with pytest.raises(ValueError, match="timezone-aware"):
        await ledger.stale_threads(saver, now=datetime.now(UTC).replace(tzinfo=None), default_minutes=60)


async def test_the_walk_pages_the_declared_lookup_and_sorts_the_ids() -> None:
    from tai42_kit.llm.checkpoint.ledger import stale_threads_by_walk

    registry = CheckpointRegistry()
    try:
        saver = await registry.get_checkpointer("memory", None)
        ledger = await registry.ledger("memory", None)
        now = datetime.now(UTC)
        thread_ids = [f"walk-{i:04d}" for i in range(2500)]
        for thread_id in reversed(thread_ids):
            await _put(saver, thread_id, now - timedelta(minutes=10))
        short = thread_ids[::2]
        await ledger.start(short, ThreadRetention(waiting_minutes=5, finished_minutes=1))
        pages: list[int] = []

        async def _declared_waiting(page: Sequence[str]) -> dict[str, int]:
            pages.append(len(page))
            return await ledger.declared_waiting(page)

        listed = await stale_threads_by_walk(saver, _declared_waiting, now=now, default_minutes=60)

        assert listed == sorted(short)
        assert pages == [1000, 1000, 500]
    finally:
        await registry.close_all()
