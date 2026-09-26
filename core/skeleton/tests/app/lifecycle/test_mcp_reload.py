"""Targeted MCP reload/deregister: result shapes, pooled-session eviction, the
serving-loop reconcile marshalling, and the on-serving-loop deadlock guard."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tai42_contract.connectors.models import ConnectorRef
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.app import lifecycle as lifecycle_module
from tai42_skeleton.connectors.runtime.resolver import ManagedAuth
from tai42_skeleton.connectors.token_injection import _prepare_request
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools import mcp_health
from tai42_skeleton.tools.adapters.mcp_tool_to_func import _detect_transport

from ._doubles import _cfg, _failed_row, _FakeMcpTool, _Mixin


def test_reload_mcp_unknown_title_is_structured_error():
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    out = m._reload_mcp("nope")
    assert out["title"] == "nope"
    assert out["status"] == "error"
    assert "Unknown MCP" in out["error"]


def test_reload_mcp_success_binds_and_clears_failed():
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._failed_mcps = {"svc": _failed_row()}
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])
    out = m._reload_mcp("svc")
    assert out["status"] == "ok"
    assert out["tools"] == ["svc_t"]
    assert "svc" not in m._failed_mcps


def test_reload_mcp_probe_failure_records_unavailable():
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._probe_mcp = AsyncMock(side_effect=TimeoutError("slow"))
    out = m._reload_mcp("svc")
    assert out == {"title": "svc", "status": "unavailable"}
    # A timeout with no HTTP status reads as an unreachable server, redacted message.
    assert m._failed_mcps["svc"] == {
        "status": "unavailable",
        "category": "unreachable",
        "message": "slow",
        "http_status": None,
    }


def test_reload_failed_mcps_keeps_siblings_on_one_failure():
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("a").model_dump(), _cfg("b").model_dump()]})
    m._failed_mcps = {"a": _failed_row(), "b": _failed_row()}
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])

    # The batch probes concurrently but applies binds one server at a time; a
    # failure applying one must not discard the other's result.
    async def fake_apply(title, config, tools):
        if title == "b":
            raise RuntimeError("rebind blew up")
        return {"title": title, "status": "ok"}

    m._apply_reloaded_mcp = fake_apply
    out = m._reload_failed_mcps()
    assert {"title": "a", "status": "ok"} in out
    assert {"title": "b", "status": "error"} in out


# -- reload evicts the pooled dispatch session --------------------------------

CONN_ID = "11111111-1111-1111-1111-111111111111"


def _managed_cfg(title="svc"):
    return TaiMCPConfig(
        title=title,
        include=[],
        config=MCPConfig(type="http", url="http://x/mcp", headers={}),
        managed=ConnectorRef(connection_id=CONN_ID, provider_id="acme", sub_service="events"),
    )


class _RecordingPool:
    """Stands in for the pooled ``FastMCPClient`` so a reload's session eviction is
    observable: records the loop it ran on and the ``close`` connection kwargs (the
    pool key a dispatch would look up)."""

    def __init__(self, sink: list[tuple[asyncio.AbstractEventLoop, dict[str, Any]]]) -> None:
        self._sink = sink

    async def close(self, **kwargs: Any) -> None:
        self._sink.append((asyncio.get_running_loop(), kwargs))


def _install_recording_pool(monkeypatch, sink):
    monkeypatch.setattr(lifecycle_module, "FastMCPClient", lambda: _RecordingPool(sink))


def test_reload_evicts_pooled_session_under_plain_key(monkeypatch):
    """A reload drops the pooled dispatch session for the title. A plain
    (non-managed) entry injects nothing, so the eviction key is the raw config
    dump — exactly the pool key a dispatch of this config uses."""
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])
    sink: list[tuple[asyncio.AbstractEventLoop, dict[str, Any]]] = []
    _install_recording_pool(monkeypatch, sink)

    out = m._reload_mcp("svc")

    assert out["status"] == "ok"
    assert [kwargs for _loop, kwargs in sink] == [{"config": _cfg("svc").model_dump()}]


def test_reload_eviction_uses_managed_effective_key(monkeypatch):
    """For a managed entry the eviction key is the auth-MERGED effective config —
    the exact key a dispatch pools under — computed through the same
    ``_prepare_request`` path, so the two can never drift."""
    m = _Mixin()
    config = _managed_cfg("svc")
    m._manifest = Manifest.model_validate({"mcp": [config.model_dump()]})
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])
    sink: list[tuple[asyncio.AbstractEventLoop, dict[str, Any]]] = []
    _install_recording_pool(monkeypatch, sink)

    auth = ManagedAuth(access_token="tok")
    monkeypatch.setattr(
        "tai42_skeleton.connectors.token_injection.resolve_managed_auth",
        AsyncMock(return_value=auth),
    )

    out = m._reload_mcp("svc")

    assert out["status"] == "ok"
    transport = _detect_transport(config.config)
    expected = _prepare_request(config, auth, transport)[0].model_dump()
    assert [kwargs for _loop, kwargs in sink] == [{"config": expected}]
    # The merged Bearer header proves the key is NOT the raw config dump.
    assert expected != config.model_dump()
    assert expected["config"]["headers"]["authorization"] == "Bearer tok"


def test_reload_eviction_runs_on_the_serving_loop(monkeypatch):
    """The ``FastMCPClient`` pools are per event loop and dispatch runs on the
    serving loop, so an admin reload whose body runs on a ``run_blocking`` worker
    loop must marshal the eviction back onto the serving loop or it misses the real
    pool."""
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])
    sink: list[tuple[asyncio.AbstractEventLoop, dict[str, Any]]] = []
    _install_recording_pool(monkeypatch, sink)

    serving_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run_serving_loop() -> None:
        asyncio.set_event_loop(serving_loop)
        serving_loop.call_soon(ready.set)
        serving_loop.run_forever()

    thread = threading.Thread(target=_run_serving_loop, daemon=True)
    thread.start()
    ready.wait()
    m._serving_loop = serving_loop
    try:
        # Called from a thread that is NOT the serving loop, as reload_gate.run's
        # worker thread would call it.
        out = m._reload_mcp("svc")
    finally:
        serving_loop.call_soon_threadsafe(serving_loop.stop)
        thread.join()
        serving_loop.close()

    assert out["status"] == "ok"
    assert [loop for loop, _kwargs in sink] == [serving_loop]


def test_reload_eviction_failure_surfaces(monkeypatch):
    """A failed session eviction is never a quiet log-and-continue: it propagates
    out of the single-server reload op."""
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])

    class _BoomPool:
        async def close(self, **kwargs: Any) -> None:
            raise RuntimeError("pool close boom")

    monkeypatch.setattr(lifecycle_module, "FastMCPClient", _BoomPool)

    with pytest.raises(RuntimeError, match="pool close boom"):
        m._reload_mcp("svc")


def test_deregister_mcp_absent_is_idempotent():
    m = _Mixin()
    assert m._deregister_mcp("never-bound") == {"title": "never-bound", "status": "absent"}


def test_deregister_mcp_bound_removes_and_reports():
    m = _Mixin()
    m._mcp_bound_tools = {"svc": {"svc_t"}}
    out = m._deregister_mcp("svc")
    assert out == {"title": "svc", "status": "ok", "removed": ["svc_t"]}
    assert "svc" not in m._mcp_bound_tools


def test_deregister_mcp_forgets_health():
    """Deregistering a title clears its passive health — the title ceases to exist,
    so it leaves no residue in the process-wide health store."""
    mcp_health._HEALTH.clear()
    m = _Mixin()
    m._mcp_bound_tools = {"svc": {"svc_t"}}
    mcp_health.record_failure("svc", RuntimeError("down"))
    assert "svc" in mcp_health._HEALTH

    m._deregister_mcp("svc")

    assert "svc" not in mcp_health._HEALTH


def test_deregister_reconcile_marshals_onto_the_serving_loop():
    """The deregister reconcile takes the ``PresetManager`` locks on the SERVING
    loop (via ``run_coroutine_threadsafe``), not the throwaway ``run_blocking``
    worker loop — so it can never contend cross-loop with a serving-loop preset
    route on the same name."""
    m = _Mixin()
    m._mcp_bound_tools = {"svc": {"svc_t"}}
    recorded: list[asyncio.AbstractEventLoop] = []

    async def record_reconcile(affected_bases: set[str]) -> None:
        recorded.append(asyncio.get_running_loop())

    m.preset_manager.reconcile_bases = record_reconcile  # pyright: ignore[reportAttributeAccessIssue]

    # Stand up a serving loop on a background thread and point the mixin at it.
    serving_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run_serving_loop() -> None:
        asyncio.set_event_loop(serving_loop)
        serving_loop.call_soon(ready.set)
        serving_loop.run_forever()

    thread = threading.Thread(target=_run_serving_loop, daemon=True)
    thread.start()
    ready.wait()
    m._serving_loop = serving_loop
    try:
        # Called from a thread that is NOT the serving loop, as reload_gate.run's
        # worker thread would call it.
        out = m._deregister_mcp("svc")
    finally:
        serving_loop.call_soon_threadsafe(serving_loop.stop)
        thread.join()
        serving_loop.close()

    assert out == {"title": "svc", "status": "ok", "removed": ["svc_t"]}
    # The reconcile ran ON the serving loop, not the run_blocking worker loop.
    assert recorded == [serving_loop]


def test_deregister_reconcile_falls_back_to_worker_loop_without_serving_loop():
    """With no serving loop bound (a pure-sync boot) nothing contends, so the
    deregister reconcile runs on the ``run_blocking`` worker loop and still
    completes."""
    m = _Mixin()
    m._mcp_bound_tools = {"svc": {"svc_t"}}
    recorded: list[asyncio.AbstractEventLoop] = []

    async def record_reconcile(affected_bases: set[str]) -> None:
        recorded.append(asyncio.get_running_loop())

    m.preset_manager.reconcile_bases = record_reconcile  # pyright: ignore[reportAttributeAccessIssue]

    assert m._serving_loop is None
    out = m._deregister_mcp("svc")
    assert out == {"title": "svc", "status": "ok", "removed": ["svc_t"]}
    # Ran on the throwaway worker loop — there is no serving loop to marshal onto.
    assert len(recorded) == 1


def test_reconcile_facets_on_serving_loop_raise_instead_of_deadlocking():
    """A reconcile-driving admin facet called from a coroutine ON the serving loop
    would freeze that loop in ``run_blocking`` while the marshaled reconcile waits
    for it — a deadlock. The guard raises loudly instead."""
    m = _Mixin()
    m._mcp_bound_tools = {"svc": {"svc_t"}}

    async def drive():
        m._serving_loop = asyncio.get_running_loop()
        for call in (
            lambda: m._reload_mcp("svc"),
            lambda: m._reload_failed_mcps(),
            lambda: m._deregister_mcp("svc"),
        ):
            with pytest.raises(RuntimeError, match="must not be called from the serving loop"):
                call()

    asyncio.run(drive())


def test_reload_mcp_unregisters_vanished_tool():
    # A re-probed MCP that no longer serves a previously-bound tool must drop it
    # from the base registry too (symmetry with deregister_mcp) — not leave it
    # stale until a full reload.
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])
    # Two tools bound before; _Mixin._mcp_tools rebinds only ``svc_t`` on reload,
    # so ``svc_old`` vanished.
    m._mcp_bound_tools = {"svc": {"svc_old", "svc_t"}}
    unregistered: list[str] = []
    m._tool_registry.unregister_tool_base = lambda name: (unregistered.append(name), [])[1]  # type: ignore[method-assign]

    out = m._reload_mcp("svc")

    assert out["status"] == "ok"
    assert out["tools"] == ["svc_t"]
    assert unregistered == ["svc_old"]
