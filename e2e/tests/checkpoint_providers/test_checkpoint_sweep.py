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
by backdating the checkpoint ``ts`` and the finished mark — no wall-clock sleeps.
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
    WAITING_MINUTES,
    checkpoint_conn_string,
)


@contextlib.asynccontextmanager
async def _harness_store(provider: str, conn_string: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """The stack's checkpoint store opened through the kit, as the SUT opens it: ``(saver, ledger, client)``.

    The harness process carries the stack's retention settings, so a thread it writes on the
    ``redis`` provider carries the same waiting TTL the SUT's writes do.
    """
    from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", str(WAITING_MINUTES))
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", str(FINISHED_MINUTES))
    reset_all_settings()
    resource, close = await create_checkpoint_resource(provider, conn_string)
    try:
        yield get_saver_from_resource(provider, resource), resource.ledger, resource.redis_client
    finally:
        await close()
        reset_all_settings()


def _thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _write_thread(saver: Any, thread_id: str, ts: datetime) -> None:
    """Persist one checkpoint for ``thread_id`` stamped with ``ts`` (the field the waiting horizon reads)."""
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = ts.isoformat()
    await saver.aput(_thread_config(thread_id), checkpoint, {"source": "input", "step": 0, "parents": {}}, {})


async def _sweep(stack: TaiStack) -> dict[str, Any]:
    return await stack.api().post("/api/checkpoints/sweep", json={})


@pytest.mark.needs("store:postgres", "files", "setting:LLM_PROVIDER_CHECKPOINT")
async def test_sweep_deletes_threads_past_each_horizon(
    checkpoint_stack: tuple[TaiStack, str], uniq: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, provider = checkpoint_stack
    conn_string = checkpoint_conn_string(provider, stack.resources)
    await _sweep(stack)

    stale_id = uniq("stale-thread")
    finished_id = uniq("finished-thread")
    fresh_id = uniq("fresh-thread")
    now = datetime.now(UTC)
    # Three hours back: well past both horizons (60 / 30 minutes), so the cutoffs are
    # unambiguous for the aged threads and the fresh one is inside both.
    aged = now - timedelta(hours=3)

    async with _harness_store(provider, conn_string, monkeypatch) as (saver, ledger, _client):
        await _write_thread(saver, stale_id, aged)
        await _write_thread(saver, finished_id, now)
        await _write_thread(saver, fresh_id, now)
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

    async with _harness_store("redis", conn_string, monkeypatch) as (saver, ledger, client):
        await _write_thread(saver, finished_id, now)
        await _write_thread(saver, waiting_id, now - timedelta(hours=3))
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
        # The sweep left the waiting thread to its TTL: still set, never extended.
        ttls_after = [await client.ttl(key) for key in waiting_keys]
        assert all(0 < after <= before for before, after in zip(ttls_before, ttls_after, strict=True))
