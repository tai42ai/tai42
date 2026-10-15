"""The checkpoint sweep's two horizons, end to end, on the postgres / sqlite / redis providers.

``sweep_checkpoints`` deletes a thread marked finished more than the finished retention ago on
every provider and, on ``postgres`` / ``sqlite``, any thread whose newest checkpoint is older
than the waiting retention (``operations/checkpoints.py``); on ``redis`` the key TTL every write
stamps does the waiting horizon, so the sweep leaves a waiting thread alone.

The spec drives the SUT's own sweep over ``POST /api/checkpoints/sweep`` once first, so the
SUT builds its checkpoint store (recording the store-format generation in the empty store);
the harness then opens the SAME store through the kit's own resource builder on the conn string
the SUT is pinned to, seeds threads — one with its newest checkpoint backdated past the waiting
horizon, one marked finished past the finished horizon, one fresh — and sweeps again. Ageing is
by backdating the checkpoint ``ts`` and the finished mark's due time — no wall-clock sleeps.
Every seeded thread carries a value above the stack's shared-value size, so where the stack stores
values once (``redis`` by default, ``postgres`` with the switch on) it holds a shared value, which
leaves with the thread.

A thread's owner may declare a shorter retention for it: the harness writes such a thread through
the kit's saver view (``redis``: its keys carry the declared waiting as their TTL) or records the
declared waiting at its start (``postgres`` / ``sqlite``: the sweep's per-thread waiting horizon).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint

from tai42_e2e.stack import TaiStack

from ._checkpoint_support import (  # pyright: ignore[reportMissingImports]
    FINISHED_MINUTES,
    SHARED_VALUE_BYTES,
    WAITING_MINUTES,
    checkpoint_conn_string,
)

# A document above the shared-value threshold whose fields are each below it.
_DOCUMENT = {"title": "document", "a": "a" * (SHARED_VALUE_BYTES // 2), "b": "b" * (SHARED_VALUE_BYTES // 2)}


@contextlib.asynccontextmanager
async def _harness_store(
    provider: str, conn_string: str, monkeypatch: pytest.MonkeyPatch, shared_values: bool | None = None
) -> AsyncIterator[Any]:
    """The stack's checkpoint store opened through the kit, as the SUT opens it: ``(saver, ledger, client, resource)``.

    The harness process carries the stack's retention and shared-value settings, so a thread it
    writes on the ``redis`` provider carries the same waiting TTL the SUT's writes do, and every
    write stores its values as the SUT's do.
    """
    from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", str(WAITING_MINUTES))
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", str(FINISHED_MINUTES))
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES", str(SHARED_VALUE_BYTES))
    if shared_values is None:
        monkeypatch.delenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUES", raising=False)
    else:
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUES", "true" if shared_values else "false")
    reset_all_settings()
    resource, close = await create_checkpoint_resource(provider, conn_string)
    try:
        yield get_saver_from_resource(provider, resource), resource.ledger, resource.redis_client, resource
    finally:
        await close()
        reset_all_settings()


def _thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _write_thread(saver: Any, thread_id: str, ts: datetime) -> None:
    """Persist one checkpoint for ``thread_id`` stamped with ``ts`` (the field the waiting horizon reads),
    carrying a document above the shared-value threshold."""
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = ts.isoformat()
    checkpoint["channel_values"] = {"document": _DOCUMENT}
    checkpoint["channel_versions"] = {"document": "1"}
    metadata: Any = {"source": "input", "step": 0, "parents": {}}
    await saver.aput(_thread_config(thread_id), checkpoint, metadata, {"document": "1"})
    tup = await saver.aget_tuple(_thread_config(thread_id))
    assert tup is not None
    assert tup.checkpoint["channel_values"]["document"] == _DOCUMENT


async def _shared_values(provider: str, resource: Any, thread_id: str) -> int:
    """How many shared values ``thread_id`` holds in the store (a provider with no value store holds none)."""
    if provider == "redis":
        pattern = f"tai42:checkpoint:value:{{{thread_id}}}:*"
        return len([key async for key in resource.redis_client.scan_iter(match=pattern)])
    if provider == "postgres":
        async with resource.handle.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) AS n FROM tai42_checkpoint_values WHERE thread_id = %s", (thread_id,))
            row = await cur.fetchone()
        return int(row["n"])
    return 0


async def _sweep(stack: TaiStack) -> dict[str, Any]:
    return await stack.api().post("/api/checkpoints/sweep", json={})


@pytest.mark.needs("store:postgres", "files", "setting:LLM_PROVIDER_CHECKPOINT")
async def test_sweep_deletes_threads_past_each_horizon(
    checkpoint_stack: tuple[TaiStack, str, bool | None], uniq: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.llm.checkpoint import shared_value_threshold

    stack, provider, shared_values = checkpoint_stack
    conn_string = checkpoint_conn_string(provider, stack.resources)
    await _sweep(stack)

    stale_id = uniq("stale-thread")
    finished_id = uniq("finished-thread")
    fresh_id = uniq("fresh-thread")
    now = datetime.now(UTC)
    # Three hours back: well past both horizons (60 / 30 minutes), so the cutoffs are
    # unambiguous for the aged threads and the fresh one is inside both.
    aged = now - timedelta(hours=3)

    async with _harness_store(provider, conn_string, monkeypatch, shared_values) as (saver, ledger, _client, resource):
        await _write_thread(saver, stale_id, aged)
        await _write_thread(saver, finished_id, now)
        await _write_thread(saver, fresh_id, now)
        expected = 1 if shared_value_threshold(provider) is not None else 0
        assert await _shared_values(provider, resource, stale_id) == expected
        await ledger.mark([finished_id], aged)
        for thread_id in (stale_id, finished_id, fresh_id):
            assert await saver.aget_tuple(_thread_config(thread_id)) is not None

        result = await _sweep(stack)
        assert result["provider"] == provider, result
        assert result["waiting_minutes"] == WAITING_MINUTES, result
        assert result["finished_minutes"] == FINISHED_MINUTES, result
        assert stale_id in result["waiting_swept"], result
        assert finished_id in result["finished_swept"], result
        assert fresh_id not in result["waiting_swept"] + result["finished_swept"], result
        assert result["swept_count"] >= 2, result
        assert result["skipped"] is None, result

        assert await saver.aget_tuple(_thread_config(stale_id)) is None, "stale thread survived the sweep"
        assert await saver.aget_tuple(_thread_config(finished_id)) is None, "finished thread survived the sweep"
        assert await saver.aget_tuple(_thread_config(fresh_id)) is not None, "fresh thread was wrongly swept"
        assert await _shared_values(provider, resource, stale_id) == 0, "a swept thread kept its shared value"
        assert await _shared_values(provider, resource, finished_id) == 0, "a swept thread kept its shared value"
        assert finished_id not in await ledger.finished_before(now, limit=10_000)


@pytest.mark.needs("store:redis", "setting:LLM_PROVIDER_CHECKPOINT", "setting:checkpoint:redis")
async def test_redis_sweep_deletes_finished_threads_and_leaves_waiting_ones_to_their_ttl(
    redis_checkpoint_stack: TaiStack, uniq: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = redis_checkpoint_stack
    conn_string = checkpoint_conn_string("redis", stack.resources)
    await _sweep(stack)

    finished_id = uniq("finished-thread")
    waiting_id = uniq("waiting-thread")
    now = datetime.now(UTC)

    async with _harness_store("redis", conn_string, monkeypatch) as (saver, ledger, client, resource):
        await _write_thread(saver, finished_id, now)
        await _write_thread(saver, waiting_id, now - timedelta(hours=3))
        assert await _shared_values("redis", resource, finished_id) == 1
        await ledger.mark([finished_id], now - timedelta(hours=3))
        waiting_keys = [key async for key in client.scan_iter(match=f"*{waiting_id}*")]
        assert waiting_keys
        ttls_before = [await client.ttl(key) for key in waiting_keys]
        assert all(0 < ttl <= WAITING_MINUTES * 60 for ttl in ttls_before), ttls_before

        result = await _sweep(stack)
        assert result["provider"] == "redis", result
        assert finished_id in result["finished_swept"], result
        assert result["waiting_swept"] == [], result
        assert result["skipped"] == "waiting horizon: provider 'redis' expires threads by their key TTL", result

        assert await saver.aget_tuple(_thread_config(finished_id)) is None, "finished thread survived the sweep"
        assert await saver.aget_tuple(_thread_config(waiting_id)) is not None, "waiting thread was wrongly swept"
        assert await _shared_values("redis", resource, finished_id) == 0, "a swept thread kept its shared value"
        assert await _shared_values("redis", resource, waiting_id) == 1, "a waiting thread lost its shared value"
        # The sweep left the waiting thread to its TTL: still set, never extended.
        ttls_after = [await client.ttl(key) for key in waiting_keys]
        assert all(0 < after <= before for before, after in zip(ttls_before, ttls_after, strict=True))


@pytest.mark.needs("store:postgres", "files", "setting:LLM_PROVIDER_CHECKPOINT")
async def test_sweep_applies_a_thread_declared_retention(
    checkpoint_stack: tuple[TaiStack, str, bool | None], uniq: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.llm.checkpoint import ThreadRetention

    stack, provider, shared_values = checkpoint_stack
    conn_string = checkpoint_conn_string(provider, stack.resources)
    await _sweep(stack)

    declared_id = uniq("declared-thread")
    undeclared_id = uniq("undeclared-thread")
    finished_id = uniq("declared-finished-thread")
    now = datetime.now(UTC)
    # Five minutes back: past the declared 2-minute waiting, inside the stack's 60-minute one.
    recent = now - timedelta(minutes=5)
    short = ThreadRetention(waiting_minutes=2, finished_minutes=1)

    async with _harness_store(provider, conn_string, monkeypatch, shared_values) as (
        saver,
        ledger,
        _client,
        _resource,
    ):
        await ledger.start([declared_id, finished_id], short)
        await _write_thread(saver, declared_id, recent)
        await _write_thread(saver, undeclared_id, recent)
        await _write_thread(saver, finished_id, now)
        await ledger.mark([finished_id], now - timedelta(seconds=1))

        result = await _sweep(stack)
        assert declared_id in result["waiting_swept"], result
        assert finished_id in result["finished_swept"], result
        assert undeclared_id not in result["waiting_swept"] + result["finished_swept"], result
        assert result["waiting_minutes"] == WAITING_MINUTES, result
        assert result["finished_minutes"] == FINISHED_MINUTES, result

        assert await saver.aget_tuple(_thread_config(declared_id)) is None, "declared thread survived the sweep"
        assert await saver.aget_tuple(_thread_config(finished_id)) is None, "finished thread survived the sweep"
        assert await saver.aget_tuple(_thread_config(undeclared_id)) is not None, "undeclared thread was swept"
        assert await ledger.declared_waiting([declared_id, finished_id]) == {}


@pytest.mark.needs("store:redis", "setting:LLM_PROVIDER_CHECKPOINT", "setting:checkpoint:redis")
async def test_redis_a_declared_waiting_is_every_key_ttl_and_a_due_mark_is_swept(
    redis_checkpoint_stack: TaiStack, uniq: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.llm.checkpoint.checkpoint import create_redis_saver_view

    stack = redis_checkpoint_stack
    conn_string = checkpoint_conn_string("redis", stack.resources)
    await _sweep(stack)

    declared_id = uniq("declared-thread")
    undeclared_id = uniq("undeclared-thread")
    now = datetime.now(UTC)

    async with _harness_store("redis", conn_string, monkeypatch) as (saver, ledger, client, resource):
        view = await create_redis_saver_view(resource, 2)
        await _write_thread(view, declared_id, now)
        await _write_thread(saver, undeclared_id, now)
        declared_keys = [key async for key in client.scan_iter(match=f"*{declared_id}*")]
        assert declared_keys
        # The thread's shared value and its index carry the declared waiting like every other key.
        assert await _shared_values("redis", resource, declared_id) == 1
        declared_ttls = [await client.ttl(key) for key in declared_keys]
        assert all(0 < ttl <= 120 for ttl in declared_ttls), declared_ttls
        undeclared_keys = [key async for key in client.scan_iter(match=f"*{undeclared_id}*")]
        assert undeclared_keys
        undeclared_ttls = [await client.ttl(key) for key in undeclared_keys]
        assert all(WAITING_MINUTES * 60 - 60 <= ttl <= WAITING_MINUTES * 60 for ttl in undeclared_ttls), undeclared_ttls

        await ledger.mark([declared_id], now - timedelta(seconds=1))
        result = await _sweep(stack)
        assert declared_id in result["finished_swept"], result
        assert await saver.aget_tuple(_thread_config(declared_id)) is None, "declared thread survived the sweep"
        await saver.adelete_thread(undeclared_id)
