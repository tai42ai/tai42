"""A checkpoint store registered from outside the kit gets every checkpoint feature from its registration alone.

The probe stores (``probe_store``) are written against the kit's public checkpoint seam only. Selected
by ``LLM_PROVIDER_CHECKPOINT``, a graph checkpoints, parks and resumes on them through the checkpoint
registry; their large values are stored once by their facts' default and the deployment's switch;
the stale-thread walk lists their aged threads and the store-format gate guards them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from tai42_kit.llm.checkpoint import (
    CHECKPOINT_STORE_FORMAT_GENERATION,
    CheckpointStoreFormatError,
    checkpoint_provider,
    create_checkpoint_resource,
)
from tai42_kit.llm.checkpoint.checkpoint_registry import CheckpointRegistry
from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.settings import reset_all_settings

from . import probe_store

DOC = {"title": "document", "a": "a" * 600, "b": "b" * 600}


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Select the probe store by the deployment setting, the shared-value size at 1 KiB."""

    def apply(provider: str = "probe", switch: str | None = None) -> None:
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", provider)
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_CONN_STRING", f"probe-{uuid.uuid4().hex}")
        monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES", "1024")
        if switch is None:
            monkeypatch.delenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUES", raising=False)
        else:
            monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUES", switch)
        reset_all_settings()

    yield apply
    reset_all_settings()


@pytest.fixture
async def registry() -> Any:
    registry = CheckpointRegistry()
    try:
        yield registry
    finally:
        await registry.close_all()


async def _deployment_saver(registry: CheckpointRegistry) -> tuple[Any, Any]:
    settings = llm_provider_settings()
    saver = await registry.get_checkpointer(settings.checkpoint, settings.checkpoint_conn_string)
    resource = await registry.resource(settings.checkpoint, settings.checkpoint_conn_string)
    return saver, resource


def _append(left: list[Any], right: list[Any]) -> list[Any]:
    return [*left, *right]


class _State(TypedDict):
    steps: Annotated[list[Any], _append]


def _first(state: _State) -> dict[str, Any]:
    return {"steps": [DOC]}


def _second(state: _State) -> dict[str, Any]:
    return {"steps": [interrupt("go on?")]}


def _graph(saver: Any) -> Any:
    builder = StateGraph(_State)
    builder.add_node("first", _first)
    builder.add_node("second", _second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    return builder.compile(checkpointer=saver)


async def test_a_graph_parks_and_resumes_on_the_store_the_deployment_selects(settings, registry) -> None:
    settings("probe")
    saver, resource = await _deployment_saver(registry)
    assert isinstance(resource.handle, probe_store.ProbeHandle)
    thread_id = f"probe-{uuid.uuid4().hex}"
    config: Any = {"configurable": {"thread_id": thread_id}}
    graph = _graph(saver)
    parked = await graph.ainvoke({"steps": []}, config)
    assert "__interrupt__" in parked
    # The large step is held once by the probe's value store, by its facts' default.
    assert len(await resource.values.held_digests(thread_id)) == 1
    final = await graph.ainvoke(Command(resume="yes"), config)
    assert final["steps"] == [DOC, "yes"]
    assert len(await resource.values.held_digests(thread_id)) == 1
    history = [snapshot async for snapshot in graph.aget_state_history(config)]
    assert any(snapshot.values.get("steps") for snapshot in history)
    assert all(step in (DOC, "yes") for snapshot in history for step in snapshot.values.get("steps", []))
    await saver.adelete_thread(thread_id)
    assert await resource.values.held_digests(thread_id) == set()


async def test_with_the_switch_off_the_value_store_is_never_called(settings, registry) -> None:
    settings("probe", switch="false")
    saver, resource = await _deployment_saver(registry)
    thread_id = f"probe-{uuid.uuid4().hex}"
    config: Any = {"configurable": {"thread_id": thread_id}}
    graph = _graph(saver)
    await graph.ainvoke({"steps": []}, config)
    final = await graph.ainvoke(Command(resume="yes"), config)
    assert final["steps"] == [DOC, "yes"]
    assert resource.handle.values.calls == []
    assert await resource.values.held_digests(thread_id) == set()


async def test_a_store_that_keeps_a_channel_once_per_version_shares_only_when_switched_on(settings, registry) -> None:
    settings("probe-versioned")
    saver, resource = await _deployment_saver(registry)
    off_thread = f"probe-{uuid.uuid4().hex}"
    graph = _graph(saver)
    await graph.ainvoke({"steps": []}, {"configurable": {"thread_id": off_thread}})
    assert await resource.values.held_digests(off_thread) == set()

    settings("probe-versioned", switch="true")
    saver, resource = await _deployment_saver(registry)
    on_thread = f"probe-{uuid.uuid4().hex}"
    config: Any = {"configurable": {"thread_id": on_thread}}
    graph = _graph(saver)
    await graph.ainvoke({"steps": []}, config)
    assert len(await resource.values.held_digests(on_thread)) == 1
    final = await graph.ainvoke(Command(resume="yes"), config)
    assert final["steps"] == [DOC, "yes"]


async def test_at_default_settings_a_checkpoint_above_the_inline_compress_bytes_reads_back(
    monkeypatch: pytest.MonkeyPatch, registry: CheckpointRegistry
) -> None:
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT", "probe")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_CONN_STRING", f"probe-{uuid.uuid4().hex}")
    for name in (
        "LLM_PROVIDER_CHECKPOINT_SHARED_VALUES",
        "LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES",
        "LLM_PROVIDER_CHECKPOINT_INLINE_COMPRESS_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_all_settings()
    try:
        saver, resource = await _deployment_saver(registry)
        shared = llm_provider_settings().checkpoint_shared_value_bytes
        compress = llm_provider_settings().checkpoint_inline_compress_bytes
        assert compress is not None
        # One channel held once; two kept inline, each below the shared-value bytes, together above the compress bytes.
        small = compress // 2 + 4096
        assert small < shared
        values = {"big": "a" * (shared + 4096), "left": "b" * small, "right": "c" * small}
        thread_id = f"probe-{uuid.uuid4().hex}"
        checkpoint = empty_checkpoint()
        versions: Any = dict.fromkeys(values, saver.get_next_version(None, None))
        checkpoint["channel_values"] = values
        checkpoint["channel_versions"] = versions
        config: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        saved = await saver.aput(config, checkpoint, {"source": "loop", "step": 0, "parents": {}}, versions)
        assert len(await resource.values.held_digests(thread_id)) == 1
        tup = await saver.aget_tuple(saved)
        assert tup.checkpoint["channel_values"] == values
    finally:
        reset_all_settings()


async def test_the_stale_thread_walk_lists_an_aged_thread_of_the_store(settings, registry) -> None:
    settings("probe")
    saver, resource = await _deployment_saver(registry)
    aged = f"aged-{uuid.uuid4().hex}"
    fresh = f"fresh-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    for thread_id, ts in ((aged, now - timedelta(minutes=120)), (fresh, now)):
        checkpoint = empty_checkpoint()
        checkpoint["ts"] = ts.isoformat()
        await saver.aput({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}, checkpoint, {}, {})
    stale = await resource.ledger.stale_threads(saver, now=now, default_minutes=60)
    assert aged in stale
    assert fresh not in stale


async def test_the_store_format_gate_records_the_generation_and_refuses_another() -> None:
    conn_string = f"probe-{uuid.uuid4().hex}"
    resource, close = await create_checkpoint_resource("probe", conn_string)
    await close()
    assert resource.handle.marker == {"generation": CHECKPOINT_STORE_FORMAT_GENERATION}
    resource.handle.marker["generation"] = 3
    with pytest.raises(CheckpointStoreFormatError, match=r"the probe checkpoint store .* \(generation 3, this version"):
        await create_checkpoint_resource("probe", conn_string)


async def test_a_store_holding_checkpoints_without_a_marker_is_refused() -> None:
    conn_string = f"probe-{uuid.uuid4().hex}"
    resource, _close = await create_checkpoint_resource("probe", conn_string)
    saver = checkpoint_provider("probe").saver(resource)
    await saver.aput({"configurable": {"thread_id": "t", "checkpoint_ns": ""}}, empty_checkpoint(), {}, {})
    resource.handle.marker.clear()
    with pytest.raises(CheckpointStoreFormatError, match="generation none"):
        await create_checkpoint_resource("probe", conn_string)
