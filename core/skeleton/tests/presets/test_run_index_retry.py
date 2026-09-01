"""The runs-index single-row invariant under the retry policy, at the real
``run_tool`` bind path.

A retried dispatch is ONE logical run: the retry loop sits INSIDE the runs-index
chokepoint (and inside the preset attribution stamp), so however many attempts a
policy takes, the run enumerates as exactly one row whose terminal outcome is
the FINAL result. Also proves a preset-backed tool inherits its base tool's
declared policy through the dispatch seam's parent walk.

The manifest importer re-imports the fixtures module on every ``start()`` (it
pops managed modules from ``sys.modules``), so the LIVE module instance — the
one whose decorators registered and whose ``calls`` counter the tool body
appends to — is read back through ``sys.modules`` inside the app context, never
through a top-level import of this test module.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from tai42_contract.agent.base import PresetSpec
from tai42_contract.channels import ChannelDeliveryError

import tai42_skeleton.runs.chokepoint as chokepoint
import tai42_skeleton.tools.retry as retry_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ..versioning.conftest import FakeVersioningPg

_FIXTURES_MODULE = "tests.presets._retry_fixtures"

_MANIFEST = {
    "tools": [{"title": "fx", "module": _FIXTURES_MODULE, "include": ["flaky_fetch"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


def _live_calls() -> list[str]:
    """The LIVE fixtures module's per-body-invocation counter (see the module
    docstring on why this goes through ``sys.modules``)."""
    return sys.modules[_FIXTURES_MODULE].calls


class _SpyStore:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.terminals: list[dict] = []

    async def insert_start(
        self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
    ):
        self.starts.append({"run_id": run_id, "preset_name": preset_name})

    async def update_outcome(self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None):
        self.terminals.append({"run_id": run_id, "outcome": outcome})


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


@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def _capture(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(retry_module, "_sleep", _capture)
    return recorded


def test_retried_preset_dispatch_is_one_run_row(pg: FakeVersioningPg, spy_store: _SpyStore, no_sleep):
    async def run() -> int:
        async with app.app_context(_manifest()):
            await _register("flaky_preset", "flaky_fetch", {"city": "paris"})
            # The preset declared no policy of its own: the base tool's rides
            # along (the preset runs the same body with baked constants).
            result = await app.tools.run_tool("flaky_preset", {})
            assert result == {"city": "paris", "attempts": 3}
            return len(_live_calls())

    body_calls = asyncio.run(run())
    # Three attempts, ONE logical dispatch: one START row, one SUCCESS terminal.
    assert body_calls == 3
    assert [s["preset_name"] for s in spy_store.starts] == ["flaky_preset"]
    assert [t["outcome"] for t in spy_store.terminals] == ["success"]
    assert len(no_sleep) == 2


def test_exhausted_preset_dispatch_is_one_error_row(pg: FakeVersioningPg, spy_store: _SpyStore, no_sleep):
    async def run() -> int:
        async with app.app_context(_manifest()):
            # More failures than the 3-attempt budget: the last error escapes.
            await _register("doomed_preset", "flaky_fetch", {"city": "x", "fail_times": 99})
            with pytest.raises(ChannelDeliveryError):
                await app.tools.run_tool("doomed_preset", {})
            return len(_live_calls())

    body_calls = asyncio.run(run())
    assert body_calls == 3
    assert [s["preset_name"] for s in spy_store.starts] == ["doomed_preset"]
    assert [t["outcome"] for t in spy_store.terminals] == ["error"]


def test_direct_base_tool_dispatch_retries_without_a_run_row(pg: FakeVersioningPg, spy_store: _SpyStore, no_sleep):
    async def run() -> int:
        async with app.app_context(_manifest()):
            # A raw (non-preset) tool call: retried under its declared policy,
            # still never a runs-index row (unchanged raw-call posture).
            result = await app.tools.run_tool("flaky_fetch", {"city": "y"})
            assert result == {"city": "y", "attempts": 3}
            return len(_live_calls())

    body_calls = asyncio.run(run())
    assert body_calls == 3
    assert spy_store.starts == []
    assert spy_store.terminals == []
