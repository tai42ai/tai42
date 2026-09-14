"""Failed-MCP recording/listing, the missing-tools ignore set, live MCP status, and
the ``_load_mcps`` probe seam (early return, budgets, pooled client)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import ClassVar, cast
from unittest.mock import AsyncMock

from tai42_skeleton.app import lifecycle as lifecycle_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools import mcp_health

from ._doubles import _cfg, _FakeMcpTool, _Mixin


def test_record_and_list_failed_mcps_strip_to_title_status():
    m = _Mixin()
    m._record_failed_mcp(_cfg("redis"), "TimeoutError")
    assert m._failed_mcps == {"redis": "unavailable"}
    assert m._list_failed_mcps() == [{"title": "redis", "status": "unavailable"}]


def test_missing_tools_ignore_maps_failed_titles_to_tool_names():
    m = _Mixin()
    m._failed_mcps = {"svc": "unavailable"}

    class _M:
        include_title_mcp_tools_map: ClassVar[dict[str, set[str]]] = {"svc": {"svc_a", "svc_b"}}

    # _missing_tools_ignore only reads include_title_mcp_tools_map; the pydantic
    # Manifest cannot be structurally matched by this minimal stand-in.
    m._manifest = cast("Manifest", _M())
    assert m._missing_tools_ignore() == frozenset({"svc_a", "svc_b"})


def test_live_mcp_status_snapshot():
    mcp_health._HEALTH.clear()
    m = _Mixin()
    m._mcp_bound_tools = {"svc": {"b", "a"}}
    m._failed_mcps = {"down": "unavailable"}
    mcp_health.record_success("svc")
    status = m._live_mcp_status()
    assert status["bound"] == {"svc": ["a", "b"]}
    assert status["failed"] == [{"title": "down", "status": "unavailable"}]
    # Every bound and failed title carries a health block.
    assert set(status["health"]) == {"svc", "down"}
    # A called MCP shows its last success; the four fields are always present.
    assert status["health"]["svc"]["last_success"] is not None
    assert status["health"]["svc"]["consecutive_failures"] == 0
    # A never-called MCP carries the block at its empty values.
    assert status["health"]["down"] == {
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
        "failing_since": None,
    }


def test_load_mcps_early_returns_without_mcp():
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    assert asyncio.run(m._load_mcps()) == ([], [])


def test_load_mcps_uses_short_reload_budget_during_a_rebuild(monkeypatch):
    # A RELOAD (epoch rebuild) probes with the SHORT budget, so an unreachable server can't
    # stall the reload gate / fleet convergence for the full cold-boot budget.
    from tai42_skeleton.settings.cache import mcp_probe_timeout, mcp_reload_probe_timeout

    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    probe = AsyncMock(return_value=[])
    monkeypatch.setattr(m, "_probe_mcp", probe)
    monkeypatch.setattr(lifecycle_module, "is_epoch_rebuild_in_progress", lambda: True)
    asyncio.run(m._load_mcps())
    call = probe.await_args
    assert call is not None  # the probe WAS awaited
    assert call.kwargs["timeout"] == mcp_reload_probe_timeout()
    assert mcp_reload_probe_timeout() < mcp_probe_timeout()  # short reload budget vs cold-boot


def test_load_mcps_uses_full_budget_at_cold_boot(monkeypatch):
    # Cold boot (no rebuild in progress) keeps the generous budget: it is one-time and may
    # legitimately wait for a server still coming up.
    from tai42_skeleton.settings.cache import mcp_probe_timeout

    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    probe = AsyncMock(return_value=[])
    monkeypatch.setattr(m, "_probe_mcp", probe)
    monkeypatch.setattr(lifecycle_module, "is_epoch_rebuild_in_progress", lambda: False)
    asyncio.run(m._load_mcps())
    call = probe.await_args
    assert call is not None  # the probe WAS awaited
    assert call.kwargs["timeout"] == mcp_probe_timeout()


def test_probe_mcp_uses_pooled_client_seam(monkeypatch):
    # The real ``_probe_mcp`` opens a fresh pooled FastMCPClient and lists tools.
    class _Client:
        async def list_tools(self):
            return [_FakeMcpTool()]

    @asynccontextmanager
    async def fake_ctx(client_cls, *args, **kwargs):
        yield _Client()

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            monkeypatch.setattr(app.clients, "client_ctx", fake_ctx)
            tools = await app._probe_mcp(_cfg("svc"))
            assert [t.name for t in tools] == ["ping"]

    asyncio.run(run())
