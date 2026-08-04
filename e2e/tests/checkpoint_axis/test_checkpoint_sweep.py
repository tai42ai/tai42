"""B5 — the deleting checkpoint sweep against the postgres / sqlite providers.

``sweep_checkpoints`` deletes every thread whose newest checkpoint is older than
``checkpoint_ttl_minutes``, and ONLY for the ``{postgres, sqlite}`` providers with a
TTL set (``operations/checkpoints.py``). This spec exercises that deleting
branch end to end: it writes two threads into this stack's checkpoint store through a
harness-side langgraph saver on the SAME conn string the SUT is pinned to — one
thread BACKDATED past the TTL, one fresh — then drives the SUT's own sweep over
``POST /api/checkpoints/sweep`` and asserts it reports the stale thread in
``swept_threads`` and that the row is really gone, while the fresh thread survives.

Ageing is by BACKDATING the checkpoint ``ts`` (the sweep compares newest ``ts`` to
``now - ttl``) — no wall-clock sleeps. The savers are the
langgraph checkpoint libraries the SUT itself lazy-imports; the harness speaks to
them directly (a library, never the SUT process) to seed and verify the store."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from _checkpoint_support import checkpoint_conn_string  # pyright: ignore[reportMissingImports]
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, empty_checkpoint

from tai42_e2e.stack import TaiStack


@contextlib.asynccontextmanager
async def _harness_saver(provider: str, conn_string: str) -> AsyncIterator[BaseCheckpointSaver]:
    """Open a harness-side langgraph saver on the stack's checkpoint store and run
    its (idempotent) schema setup, so the spec can seed and verify rows the SUT sweep
    walks. The SUT opens its own saver on the same conn string; both call ``setup``,
    which is idempotent, so first-writer-creates-tables holds regardless of order."""
    if provider == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
            await saver.setup()
            yield saver
    elif provider == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(conn_string) as saver:
            await saver.setup()
            yield saver
    else:
        raise ValueError(f"unsupported checkpoint provider: {provider!r}")


def _thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _write_thread(saver: BaseCheckpointSaver, thread_id: str, ts: str) -> None:
    """Persist one checkpoint for ``thread_id`` stamped with ``ts`` (the field the
    sweep reads for staleness), through the saver's own put surface."""
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = ts
    await saver.aput(_thread_config(thread_id), checkpoint, {}, {})


async def test_sweep_deletes_backdated_threads(
    checkpoint_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, provider = checkpoint_stack
    conn_string = checkpoint_conn_string(provider, stack.resources)

    stale_id = uniq("stale-thread")
    fresh_id = uniq("fresh-thread")
    now = datetime.now(UTC)
    # Backdated three hours: well past the profile's 60-minute idle TTL, so the sweep's
    # ``ts < now - ttl`` test is unambiguously true for the stale thread and false for
    # the fresh one — no wall-clock wait races the cutoff.
    stale_ts = (now - timedelta(hours=3)).isoformat()
    fresh_ts = now.isoformat()

    async with _harness_saver(provider, conn_string) as saver:
        await _write_thread(saver, stale_id, stale_ts)
        await _write_thread(saver, fresh_id, fresh_ts)
        # Both rows are present before the sweep — the seed really landed.
        assert await saver.aget_tuple(_thread_config(stale_id)) is not None
        assert await saver.aget_tuple(_thread_config(fresh_id)) is not None

        result = await stack.api().post("/api/checkpoints/sweep", json={})
        assert result["provider"] == provider, result
        assert result["ttl_minutes"] == 60, result
        assert stale_id in result["swept_threads"], result
        assert fresh_id not in result["swept_threads"], result
        assert result["swept_count"] >= 1, result

        # The deleting branch really removed the stale thread and left the fresh one.
        assert await saver.aget_tuple(_thread_config(stale_id)) is None, "stale thread survived the sweep"
        assert await saver.aget_tuple(_thread_config(fresh_id)) is not None, "fresh thread was wrongly swept"
