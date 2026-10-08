"""A neutral consumer of the checkpoint retention seams: the finished-thread ledger and the live-thread filters.

A test-only two-node graph — no platform consumer's shape — mints a thread, pauses on an
interrupt, resumes and finishes, then marks its thread finished through the kit. With the clock
past the finished horizon the platform sweep deletes it and leaves an unmarked thread alone; a
filter the consumer registers under ``probe`` spares a marked thread it claims (reported in
``spared``), and a thread the consumer marks active again is not swept. Runs on the in-process
and sqlite stores, and on real Redis / Postgres when they are configured.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from tai42_kit.llm.checkpoint import (
    liveness,
    mark_threads_active,
    mark_threads_finished,
    register_live_thread_filter,
)
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.operations import checkpoints as checkpoints_ops


def _append(left: list[str], right: list[str]) -> list[str]:
    return [*left, *right]


class ProbeState(TypedDict):
    steps: Annotated[list[str], _append]


def _first(state: ProbeState) -> dict[str, Any]:
    return {"steps": ["first"]}


def _second(state: ProbeState) -> dict[str, Any]:
    return {"steps": [interrupt("go on?")]}


async def _run_to_finish(saver: Any, thread_id: str) -> None:
    builder = StateGraph(ProbeState)
    builder.add_node("first", _first)
    builder.add_node("second", _second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    graph = builder.compile(checkpointer=saver)
    config: Any = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke({"steps": []}, config)
    final = await graph.ainvoke(Command(resume="yes"), config)
    assert final["steps"] == ["first", "yes"]


async def _thread_exists(saver: Any, thread_id: str) -> bool:
    async for _ in saver.alist({"configurable": {"thread_id": thread_id}}, limit=1):
        return True
    return False


class _Later(datetime):
    """The sweep's clock, two days ahead — past the finished horizon, inside the waiting one."""

    @classmethod
    def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
        return datetime.now(tz) + timedelta(days=2)


def _store(request: pytest.FixtureRequest, tmp_path: Any) -> tuple[str, str | None]:
    provider = request.param
    if provider == "sqlite":
        pytest.importorskip("aiosqlite")
        return provider, str(tmp_path / "probe.db")
    if provider == "redis":
        url = os.environ.get("TAI42_SKELETON_REAL_REDIS_URL")
        if not url:
            pytest.skip("real-Redis consumer test is opt-in: set TAI42_SKELETON_REAL_REDIS_URL to a Redis 8 URL")
        return provider, url
    if provider == "postgres":
        if os.environ.get("TAI42_SKELETON_REAL_PG") not in ("1", "true", "True"):
            pytest.skip("real-Postgres consumer test is opt-in: set TAI42_SKELETON_REAL_PG=1 and the PG env")
        from tai42_kit.db import database_settings

        return provider, database_settings("default").pg_dsn
    return provider, None


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("redis", marks=pytest.mark.integration),
        pytest.param("postgres", marks=pytest.mark.integration),
    ]
)
async def deployment_store(
    request: pytest.FixtureRequest, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[str, str | None]]:
    provider, conn_string = _store(request, tmp_path)
    monkeypatch.setattr(liveness, "_filters", {})
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", provider)
    if conn_string is not None:
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_CONN_STRING", conn_string)
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", str(7 * 24 * 60))
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", str(24 * 60))
    reset_all_settings()
    registry = checkpoint_registry()
    try:
        yield provider, conn_string
    finally:
        await registry.close_all()
        reset_all_settings()


async def test_a_neutral_consumer_marks_spares_and_reopens_its_threads(
    deployment_store: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, conn_string = deployment_store
    saver = await checkpoint_registry().get_checkpointer(provider, conn_string)
    run = uuid.uuid4().hex[:10]
    done, claimed, reopened, untouched = (f"probe-{name}-{run}" for name in ("done", "claimed", "reopened", "open"))
    for thread_id in (done, claimed, reopened, untouched):
        await _run_to_finish(saver, thread_id)

    await mark_threads_finished([done, claimed, reopened])
    await mark_threads_active([reopened])

    async def _probe(provider: str, conn_string: str | None, thread_ids: Sequence[str]) -> set[str]:
        return {thread_id for thread_id in thread_ids if thread_id == claimed}

    register_live_thread_filter("probe", _probe)
    monkeypatch.setattr(checkpoints_ops, "datetime", _Later)

    result = await checkpoints_ops.sweep_checkpoints()

    assert done in result["finished_swept"]
    assert claimed in result["spared"]
    assert reopened not in result["finished_swept"]
    assert not await _thread_exists(saver, done)
    for kept in (claimed, reopened, untouched):
        assert await _thread_exists(saver, kept)
    # The spared thread stays marked, so the next sweep checks it again.
    ledger = await checkpoint_registry().ledger(provider, conn_string)
    assert claimed in await ledger.finished_before(datetime.now(UTC) + timedelta(days=2), limit=10_000)
    for thread_id in (claimed, reopened, untouched):
        await saver.adelete_thread(thread_id)


async def test_a_sweep_whose_only_due_thread_is_claimed_reports_it_spared(
    deployment_store: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, conn_string = deployment_store
    saver = await checkpoint_registry().get_checkpointer(provider, conn_string)
    ledger = await checkpoint_registry().ledger(provider, conn_string)
    horizon = datetime.now(UTC) + timedelta(days=2)
    # The shared stores may hold marks other tests left; the sweep must see this thread alone.
    leftover = await ledger.finished_before(horizon, limit=10_000)
    if leftover:
        await ledger.forget(leftover)
    claimed = f"probe-alone-{uuid.uuid4().hex[:10]}"
    await _run_to_finish(saver, claimed)
    await mark_threads_finished([claimed])

    async def _probe(provider: str, conn_string: str | None, thread_ids: Sequence[str]) -> set[str]:
        return {thread_id for thread_id in thread_ids if thread_id == claimed}

    register_live_thread_filter("probe", _probe)
    monkeypatch.setattr(checkpoints_ops, "datetime", _Later)
    try:
        result = await checkpoints_ops.sweep_checkpoints()

        assert result["spared"] == [claimed]
        assert result["finished_swept"] == []
        assert await _thread_exists(saver, claimed)
        assert await ledger.finished_before(horizon, limit=10_000) == [claimed]
    finally:
        await saver.adelete_thread(claimed)
        await ledger.forget([claimed])


@pytest.mark.integration
@pytest.mark.parametrize("deployment_store", ["redis"], indirect=True)
async def test_the_sweep_leaves_no_document_of_a_finished_thread_with_more_writes_than_one_search_page(
    deployment_store: tuple[str, str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    provider, conn_string = deployment_store
    saver: Any = await checkpoint_registry().get_checkpointer(provider, conn_string)
    thread_id = f"probe-large-{uuid.uuid4().hex[:10]}"
    config: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    saved = await saver.aput(config, empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {})
    for task in range(11):
        await saver.aput_writes(saved, [(f"c{index}", index) for index in range(1000)], task_id=f"task-{task}")
    await mark_threads_finished([thread_id])
    monkeypatch.setattr(checkpoints_ops, "datetime", _Later)

    result = await checkpoints_ops.sweep_checkpoints()

    assert thread_id in result["finished_swept"]
    remaining = [key async for key in saver._redis.scan_iter(match=f"*{thread_id}*", count=5000)]
    assert remaining == []
