"""The runs-index write GATE at the real ``run_tool`` chokepoint.

Drives the true bind path (``app.app_context`` + the live tool registry over the fake
Postgres) so the outermost-only decision is exercised end-to-end: one row per OUTERMOST
registered-preset dispatch, a NESTED sub-preset dispatch writes NO second row, and a
raw (non-preset) tool call writes none. The runs-index store is a spy (the ``run_index``
table itself is covered by ``tests/runs/test_store.py``); the trace-id sample is stubbed
so the gate test carries no monitoring dependency.
"""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.agent.base import PresetSpec

import tai42_skeleton.runs.chokepoint as chokepoint
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "tools": [{"title": "fx", "module": "tests.presets._run_index_fixtures", "include": ["leaf", "redispatch"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


class _SpyStore:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.terminals: list[dict] = []

    async def insert_start(
        self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
    ):
        self.starts.append(
            {
                "run_id": run_id,
                "preset_name": preset_name,
                "preset_version": preset_version,
                "interaction_id": interaction_id,
            }
        )

    async def update_outcome(self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None):
        self.terminals.append({"run_id": run_id, "outcome": outcome, "interaction_id": interaction_id})


async def _register(name: str, base_tool: str, fixed_kwargs: dict) -> None:
    await app.presets.store.create_preset(
        PresetSpec(name=name, description="d", base_tool=base_tool, fixed_kwargs=fixed_kwargs),
        extensions=[],
    )
    body = await app.presets.store.get_active_body(name)
    await app.preset_manager.register(name, body.base_tool, body.fixed_kwargs, body.extensions, body.description)


@pytest.fixture
def spy_store(monkeypatch) -> _SpyStore:
    store = _SpyStore()
    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: store)
    monkeypatch.setattr(chokepoint, "_safe_trace_id", lambda: "trace-x")
    return store


def test_outermost_preset_dispatch_writes_one_row(pg: FakeVersioningPg, spy_store: _SpyStore):
    async def run():
        async with app.app_context(_manifest()):
            await _register("inner", "leaf", {"city": "paris"})
            assert await app.tools.run_tool("inner", {}) == {"city": "paris"}

    asyncio.run(run())
    assert [s["preset_name"] for s in spy_store.starts] == ["inner"]
    assert [t["outcome"] for t in spy_store.terminals] == ["success"]


def test_nested_sub_preset_dispatch_writes_only_one_row(pg: FakeVersioningPg, spy_store: _SpyStore):
    async def run():
        async with app.app_context(_manifest()):
            await _register("inner", "leaf", {"city": "paris"})
            await _register("outer", "redispatch", {"target": "inner"})
            # ``outer`` dispatches ``redispatch``, whose body re-enters run_tool on
            # ``inner`` — a nested preset dispatch.
            assert await app.tools.run_tool("outer", {}) == {"city": "paris"}

    asyncio.run(run())
    # Exactly ONE row — the OUTERMOST (``outer``); the nested ``inner`` dispatch saw the
    # armed guard and wrote nothing.
    assert [s["preset_name"] for s in spy_store.starts] == ["outer"]
    assert len(spy_store.terminals) == 1


def test_raw_tool_call_writes_no_row(pg: FakeVersioningPg, spy_store: _SpyStore):
    async def run():
        async with app.app_context(_manifest()):
            assert await app.tools.run_tool("leaf", {"city": "x"}) == {"city": "x"}

    asyncio.run(run())
    assert spy_store.starts == []
    assert spy_store.terminals == []


def test_resume_origin_deposit_reaches_the_start_row_through_run_tool(pg: FakeVersioningPg, spy_store: _SpyStore):
    # The lifecycle-correlation seam end-to-end: a preset dispatched under the
    # ambient ``resume_origin`` deposit (what the interactions continuation drive
    # lays around its ``run_tool`` re-entry) records that interaction id on its
    # START row; a plain dispatch records NULL.
    from tai42_skeleton.runs.chokepoint import resume_origin

    async def run():
        async with app.app_context(_manifest()):
            await _register("inner", "leaf", {"city": "paris"})
            with resume_origin("i-park-7"):
                assert await app.tools.run_tool("inner", {}) == {"city": "paris"}
            assert await app.tools.run_tool("inner", {}) == {"city": "paris"}

    asyncio.run(run())
    assert [s["interaction_id"] for s in spy_store.starts] == ["i-park-7", None]
