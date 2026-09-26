"""Failed-MCP recording/listing, the missing-tools ignore set, live MCP status, and
the ``_load_mcps`` probe seam (early return, budgets, pooled client)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import ClassVar, cast
from unittest.mock import AsyncMock

import httpx

from tai42_skeleton.app import lifecycle as lifecycle_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools import mcp_health

from ._doubles import _cfg, _failed_row, _FakeMcpTool, _Mixin


class _Resp:
    """A minimal stand-in for an ``httpx`` response carrying a status."""

    def __init__(self, status: int) -> None:
        self.status_code = status


class _HttpError(Exception):
    """A probe error carrying an HTTP status the way an ``httpx`` response error does."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.response = _Resp(status)


def test_record_failed_mcp_records_credential_free_detail():
    m = _Mixin()
    m._record_failed_mcp(
        _cfg("gh"),
        _HttpError("Client error '401 Unauthorized' for url 'https://user:p4ss@h/sse?apikey=leakval'", 401),
    )
    row = m._failed_mcps["gh"]
    assert row["status"] == "unavailable"
    assert row["category"] == "auth"  # a 401 reads as auth, never an outage
    assert row["http_status"] == 401
    # The URL userinfo and query values are redacted — no credential rides the record.
    assert "p4ss" not in row["message"]
    assert "leakval" not in row["message"]
    assert "<redacted>" in row["message"]
    # The list door surfaces the same record under a ``title`` key, nothing more.
    assert m._list_failed_mcps() == [{"title": "gh", **row}]


def test_failed_mcp_categories_cover_auth_unreachable_error():
    m = _Mixin()
    m._record_failed_mcp(_cfg("a"), _HttpError("forbidden", 403))
    m._record_failed_mcp(_cfg("b"), TimeoutError("slow"))
    m._record_failed_mcp(_cfg("c"), httpx.ConnectError("no route to host"))
    m._record_failed_mcp(_cfg("d"), _HttpError("server error", 500))
    m._record_failed_mcp(_cfg("e"), RuntimeError("something odd"))
    categories = {title: m._failed_mcps[title]["category"] for title in ("a", "b", "c", "d", "e")}
    assert categories == {"a": "auth", "b": "unreachable", "c": "unreachable", "d": "error", "e": "error"}
    assert m._failed_mcps["b"]["http_status"] is None
    assert m._failed_mcps["d"]["http_status"] == 500


def test_record_failed_mcp_reads_status_through_a_wrapped_cause():
    # A probe error is often a client error wrapping an httpx response error; the
    # status and category are read across the cause chain, not just the outermost.
    m = _Mixin()
    try:
        try:
            raise _HttpError("Unauthorized", 401)
        except _HttpError as inner:
            raise RuntimeError("probe handshake failed") from inner
    except RuntimeError as wrapped:
        m._record_failed_mcp(_cfg("wrapped"), wrapped)
    assert m._failed_mcps["wrapped"]["category"] == "auth"
    assert m._failed_mcps["wrapped"]["http_status"] == 401


def test_missing_tools_ignore_maps_failed_titles_to_tool_names():
    m = _Mixin()
    m._failed_mcps = {"svc": _failed_row()}

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
    m._failed_mcps = {"down": _failed_row(category="unreachable", message="slow")}
    mcp_health.record_success("svc")
    status = m._live_mcp_status()
    assert status["bound"] == {"svc": ["a", "b"]}
    assert status["failed"] == [
        {"title": "down", "status": "unavailable", "category": "unreachable", "message": "slow", "http_status": None}
    ]
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
