"""A profile apply whose recycle roll stopped answers 200 with its report on every door.

The assembled app (``create_app``) runs under uvicorn on a loopback port with the real
access-control stack, the config router and ``apply_profile`` projected as an MCP tool.
The REAL apply pipeline runs over in-memory fakes (store, bus); only its build and its
recycle orchestration are injected, the orchestration stopping on a target that never
converged as the live roll does. The env is persisted by then, so the HTTP route, the
MCP ``tools/call`` and the in-process ``run_tool`` all read the report, with the row the
roll stopped at, and no door arms the applier's own self-exit.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_contract.settings_profiles import SettingsProfileBody

# ``bus_settings`` is imported for its registration side effect — the apply's recycle
# classification reflects only IMPORTED settings classes, and TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.bus import WorkerRow
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.recycle import (
    RECYCLED,
    TIMED_OUT,
    RecycleReport,
    RecycleRow,
    RecycleStop,
    RecycleTimeoutError,
    orchestrate_recycle,
)
from tai42_skeleton.config.service import ConfigService, ProfileApplyOutcome
from tai42_skeleton.operations import config as config_ops

from .._fakes.bus import FakeBus
from ..config.fake_pipeline import FakeConfigStore, FakeReloadAdmin
from ._served_app import (
    ADMIN_KEY,
    REQUEST_TIMEOUT_S,
    TOOL_MANIFEST_ENTRY,
    bearer,
    install_access_control,
    served,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the SSE
    # response iterates to its end and never closes; anyio reports that reader as unclosed
    # when it is freed. A third-party resource report, matched narrowly.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

PROFILE = "next"
STOP_DETAIL = (
    "old life still present; the recycle op was not confirmed by the target (no received-ack within the ack "
    "timeout while presence stayed live — worker missing (alive but silent))"
)
SERVED_MANIFEST: dict[str, Any] = {
    "default_routers": "none",
    "routers_modules": ["tai42_skeleton.routers.config"],
    "tools": [TOOL_MANIFEST_ENTRY],
    "api_tools": {"enabled": True, "include": ["apply_profile"]},
}


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    install_access_control(monkeypatch)


@pytest.fixture(autouse=True)
def _process_state_restored(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore what a boot leaves on the process: the env it applied and the registry's records."""
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()


class _ProfileStore:
    async def get_active_body(self, name: str) -> SettingsProfileBody:
        assert name == PROFILE
        return SettingsProfileBody(env={"TAI_BUS_NAMESPACE": "new"}, secret_keys=[])


class _DroppingBus(FakeBus):
    """A fake bus that answers the census once (the apply's pre-swap read) and then drops:
    every later census read raises the transport error a refused connection gives."""

    def __init__(self) -> None:
        super().__init__(origin="serve-applier")
        self._reads = 0

    async def census(self) -> list[WorkerRow]:
        self._reads += 1
        if self._reads > 1:
            raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return await super().census()


class _Pipeline(ConfigService):
    """The real apply pipeline over fakes; the ``@previous`` snapshot and the build do
    nothing. The roll either stops at ``backend-1`` after recycling ``backend-0``, exactly
    as the orchestration raises, or (``outage``) is the REAL orchestration over a bus that
    drops after the apply's pre-swap census read."""

    applied: list[ProfileApplyOutcome]
    outage: bool

    async def apply_replace_env(self, profile_env: dict[str, str], **kwargs: Any) -> ProfileApplyOutcome:
        async def swap(env: dict[str, str], *, drain_tolerate_driver: bool) -> Epoch:
            return Epoch(number=0)

        async def stopping_roll(*_args: Any, **_kwargs: Any) -> RecycleReport:
            partial = RecycleReport(
                rows=[
                    RecycleRow(name="backend-0", kind="backend", generation_before=1, status=RECYCLED),
                    RecycleRow(
                        name="backend-1", kind="backend", generation_before=1, status=TIMED_OUT, detail=STOP_DETAIL
                    ),
                ],
                stopped=RecycleStop(kind="backend", name="backend-1", detail=STOP_DETAIL),
            )
            raise RecycleTimeoutError("backend-1", "old life still present", partial)

        async def save_previous(_stored: dict[str, str]) -> None:
            return None

        outcome = await super().apply_replace_env(
            profile_env,
            driven=kwargs["driven"],
            save_previous=save_previous,
            build_and_swap=swap,
            orchestrate=orchestrate_recycle if self.outage else stopping_roll,
        )
        self.applied.append(outcome)
        return outcome


def _install_pipeline(
    monkeypatch: pytest.MonkeyPatch, *, outage: bool = False
) -> tuple[list[FakeConfigStore], list[ProfileApplyOutcome]]:
    """Each door's apply gets a fresh store holding the old env, so every apply carries the recycle-class diff."""
    monkeypatch.setenv("TAI_SUPERVISED", "harness")
    stores: list[FakeConfigStore] = []
    applied: list[ProfileApplyOutcome] = []

    def from_app(_cls: type[ConfigService]) -> ConfigService:
        store = FakeConfigStore(env={"TAI_BUS_NAMESPACE": "old"})
        stores.append(store)
        bus = _DroppingBus() if outage else FakeBus(origin="serve-applier")
        pipeline = _Pipeline(config_manager=store, admin=FakeReloadAdmin(), bus=cast("Any", bus))
        pipeline.applied = applied
        pipeline.outage = outage
        return pipeline

    monkeypatch.setattr(config_ops, "_require_profile_store", lambda: None)
    monkeypatch.setattr(config_ops, "_profile_store", lambda: _ProfileStore())
    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(from_app))
    return stores, applied


def _assert_report(report: dict[str, Any]) -> None:
    assert report["recycle"] == [
        {"name": "backend-0", "kind": "backend", "status": "recycled", "generation_before": 1, "detail": None},
        {"name": "backend-1", "kind": "backend", "status": "timed-out", "generation_before": 1, "detail": STOP_DETAIL},
    ]
    # The roll stopped: no self-deferred line although the diff is serve-affecting.
    assert all(entry["status"] != "self-deferred" for entry in report["recycle"])
    assert report["refused"] == []
    assert report["recycle_stopped"] == {"kind": "backend", "name": "backend-1", "detail": STOP_DETAIL}


async def _mcp_call(url: str, name: str, arguments: dict[str, Any]) -> Any:
    client = httpx.AsyncClient(headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
    async with (
        asyncio.timeout(REQUEST_TIMEOUT_S),
        client,
        streamable_http_client(url, http_client=client) as (read, write, _session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(name, arguments)


async def test_a_stopped_roll_answers_200_with_its_report_on_every_door(monkeypatch: pytest.MonkeyPatch) -> None:
    stores, applied = _install_pipeline(monkeypatch)
    async with served(monkeypatch, SERVED_MANIFEST) as server:
        # HTTP door (the CLI rides it).
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(
                f"{server.base_url}/api/config/profiles/{PROFILE}/apply", headers=bearer(ADMIN_KEY)
            )
        assert response.status_code == 200, response.text
        _assert_report(response.json()["data"])

        # MCP door: the projected tool's result carries the same report.
        result = await _mcp_call(f"{server.base_url}/mcp", "apply_profile", {"name": PROFILE})
        assert result.isError is False, result.content
        assert result.structuredContent is not None
        _assert_report(cast("dict[str, Any]", result.structuredContent))

        # In-process door: a caller of the projected tool reads the report as its result.
        _assert_report(await app.tools.run_tool("apply_profile", {"name": PROFILE}))

    assert len(applied) == 3
    assert all(outcome.self_exit_armed is False for outcome in applied)
    # Every door's env persisted before its roll ran.
    assert [store.env for store in stores] == [{"TAI_BUS_NAMESPACE": "new"}] * 3


def _assert_outage_report(report: dict[str, Any]) -> None:
    # The bus dropped before the roll could read the census: no target was in hand, so
    # no row — the stop says where and why instead of the body reading as a complete roll.
    assert report["recycle"] == []
    stopped = report["recycle_stopped"]
    assert stopped["kind"] == "backend"
    assert stopped["name"] is None
    assert "ConnectionError" in stopped["detail"]
    assert "Error 111" in stopped["detail"]
    assert "fanout" in report


async def test_a_bus_outage_during_the_roll_answers_200_with_the_stop_on_every_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores, applied = _install_pipeline(monkeypatch, outage=True)
    async with served(monkeypatch, SERVED_MANIFEST) as server:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(
                f"{server.base_url}/api/config/profiles/{PROFILE}/apply", headers=bearer(ADMIN_KEY)
            )
        assert response.status_code == 200, response.text
        _assert_outage_report(response.json()["data"])

        result = await _mcp_call(f"{server.base_url}/mcp", "apply_profile", {"name": PROFILE})
        assert result.isError is False, result.content
        assert result.structuredContent is not None
        _assert_outage_report(cast("dict[str, Any]", result.structuredContent))

        _assert_outage_report(await app.tools.run_tool("apply_profile", {"name": PROFILE}))

    assert len(applied) == 3
    assert all(outcome.self_exit_armed is False for outcome in applied)
    assert [store.env for store in stores] == [{"TAI_BUS_NAMESPACE": "new"}] * 3
