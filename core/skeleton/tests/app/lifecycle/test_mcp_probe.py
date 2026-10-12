"""Failed-MCP recording/listing, the missing-tools ignore set, live MCP status, and
the ``_load_mcps`` probe seam (early return, budgets, pooled client)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from tai42_contract.connectors.models import AuthHealthState, ConnectorRef
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.app import lifecycle as lifecycle_module
from tai42_skeleton.app.clients import ClientsFacet
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools import mcp_health

from ...connectors import mcp_fakes
from ...connectors.credential_path import CONNECTION_ID, CredentialPath, on_loop, owner_loop
from ._credential_app import BUDGET, roomy_budgets
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
    assert "https://***@h/sse?apikey=***" in row["message"]
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


class _UnusableSchemaTool:
    """An MCP tool whose empty union crashes the schema converter — the unusable-schema
    case the per-tool skip contains and the reload result must name."""

    name = "broken"
    description = "malformed"
    inputSchema: ClassVar[dict] = {"anyOf": []}
    outputSchema: ClassVar[dict] = {}
    title = None
    annotations = None
    icons = None


class _UsableSchemaTool:
    name = "good"
    description = "ok"
    inputSchema: ClassVar[dict] = {"type": "object", "properties": {"q": {"type": "string"}}}
    outputSchema: ClassVar[dict] = {}
    title = None
    annotations = None
    icons = None


def test_unusable_schema_tool_named_in_skipped_tools(monkeypatch):
    # A server advertising one unusable-schema tool + one usable one: the reload result
    # NAMES the skipped tool (``skipped_tools``) and still binds the usable one — the skip
    # is operator-visible on the result, not only in the log.
    import mcp

    from tai42_skeleton.app.reload_gate import reload_gate

    manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    probe = AsyncMock(
        return_value=[
            cast(mcp.types.Tool, _UsableSchemaTool()),
            cast(mcp.types.Tool, _UnusableSchemaTool()),
        ]
    )
    monkeypatch.setattr(app, "_probe_mcp", probe)

    async def run():
        async with app.app_context(manifest):
            result = await reload_gate.run(lambda: app.admin.reload_mcp("svc"), reimports=False)

            assert result["status"] == "ok"
            assert result["skipped_tools"] == ["svc_broken"]
            assert "svc_good" in result["tools"]
            assert "svc_broken" not in result["tools"]

    asyncio.run(run())


# -- the listing through the managed-credential seam ----------------------------------------------
#
# A managed OAuth entry over http whose real loopback server either lists without a credential or
# demands one; the connection's sealed record sits under the real resolver; every request, store
# read, lock take and token request is counted (``credential_path``).


def _mixin(*entries: TaiMCPConfig) -> _Mixin:
    m = _Mixin()
    m.clients = ClientsFacet()
    m._manifest = Manifest.model_validate({"mcp": [e.model_dump() for e in entries]})
    return m


def _probe(m: _Mixin, config: TaiMCPConfig) -> Any:
    """The probe as the boot listing runs it: a throwaway loop, no refresh owner bound."""
    return run_blocking(lambda: m._probe_mcp(config, timeout=BUDGET))


def _record(m: _Mixin, config: TaiMCPConfig) -> dict:
    """Probe ``config`` and record a failure as every door does; return the failure record (or ``{}``)."""
    try:
        _probe(m, config)
    except Exception as exc:
        m._record_failed_mcp(config, exc)
    return m._failed_mcps.get(config.title, {})


def test_a_server_that_demands_a_credential_lists_with_the_stored_one(path):
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old"))
    tools = _probe(_mixin(path.entry), path.entry)
    assert [t.name for t in tools] == ["ping"]
    assert path.attempts == [None, "Bearer at-old"]
    assert path.locks == 0
    assert path.token_requests == 0
    assert path.store.writes == 0


def test_an_expired_token_is_refreshed_once_and_the_tools_list(path):
    path.connect(expires_in=-5)
    path.token_endpoint()
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    m = _mixin(path.entry)
    with owner_loop() as loop:
        tools = on_loop(loop, m._probe_mcp(path.entry, timeout=BUDGET * 4))
    assert [t.name for t in tools] == ["ping"]
    assert path.attempts == [None, "Bearer at-new"]
    assert path.token_requests == 1


def test_a_managed_oauth_stdio_entry_is_listed_with_no_resolve(path, monkeypatch):
    path.connect(health=AuthHealthState.RECONNECT_REQUIRED)
    stdio = TaiMCPConfig(
        title="acme_mail_work",
        config=MCPConfig(type="stdio", command="uvx", args=["acme-mcp"]),
        managed=ConnectorRef(connection_id=CONNECTION_ID, provider_id="acme", sub_service="mail"),
    )
    opened: list[dict] = []

    class _Client:
        async def list_tools(self):
            return [_FakeMcpTool()]

    @asynccontextmanager
    async def fake_ctx(client_cls, *args, **kwargs):
        opened.append(kwargs["config"])
        yield _Client()

    m = _mixin(stdio)
    monkeypatch.setattr(m.clients, "client_ctx", fake_ctx)
    assert [t.name for t in _probe(m, stdio)] == ["ping"]
    assert opened == [stdio.model_dump()]
    assert path.loads == 0


@pytest.mark.parametrize("server_policy", ["anonymous", "demands"])
def test_a_managed_no_auth_entry_gets_one_request_and_no_store_read(path, server_policy):
    from tai42_contract.connectors.providers import ConfigFieldSpec, McpServerDescriptor, SubServiceDescriptor
    from tai42_contract.connectors.providers import ProviderDescriptor as Descriptor

    from tai42_skeleton.connectors.providers import registry

    registry.register_connector(
        Descriptor(
            id="statics",
            display_name="Statics",
            kind="none",
            origin="system",
            category="data",
            config_fields=[ConfigFieldSpec(key="api_key", label="Key", target="header")],
            sub_services={
                "main": SubServiceDescriptor(
                    id="main", display_name="Main", mcp_server=McpServerDescriptor(type="http", url=path.mcp.url)
                )
            },
        )
    )
    entry = TaiMCPConfig(
        title="statics_main_x",
        config=MCPConfig(type="http", url=path.mcp.url),
        managed=ConnectorRef(connection_id=CONNECTION_ID, provider_id="statics", sub_service="main"),
    )
    path.mcp.reset(mcp_fakes.anonymous if server_policy == "anonymous" else mcp_fakes.bearer_only("x"))
    m = _mixin(entry)
    record = _record(m, entry)
    assert path.attempts == [None]
    assert path.loads == 0
    # A server without sign-in is never noted as demanding a credential: no step brings one up to date.
    assert entry.title not in m._mcp_credential_demanded
    if server_policy == "anonymous":
        assert record == {}
    else:
        assert (record["category"], record["http_status"]) == ("auth", 401)


def test_a_hand_written_entry_gets_one_request_and_no_credential(path):
    hand = TaiMCPConfig(title="hand", config=MCPConfig(type="http", url=path.mcp.url))
    path.mcp.reset(mcp_fakes.bearer_only("x"))
    record = _record(_mixin(hand), hand)
    assert (record["category"], record["http_status"]) == ("auth", 401)
    assert path.attempts == [None]
    assert path.loads == 0


_ANONYMOUS_CASES = [
    "fresh token",
    "expired token, token endpoint never answers",
    "lock held by another holder",
    "store unreachable",
    "reconnect required",
    "refresh cooldown",
    "record missing",
]


@pytest.mark.parametrize("case", _ANONYMOUS_CASES)
def test_a_server_that_lists_without_a_credential_gets_one_request_and_no_store_lock_or_refresh(path, case):
    path.connect(expires_in=-5 if case.startswith("expired") else 3600)
    path.token_endpoint(delay=30)
    if case == "lock held by another holder":
        path.lock_held = True
    if case == "store unreachable":
        path.store.unreachable = True
    if case == "reconnect required":
        path.connect(health=AuthHealthState.RECONNECT_REQUIRED)
    if case == "refresh cooldown":
        path.cooldown = True
    if case == "record missing":
        path.store.blob = None
    path.mcp.reset(mcp_fakes.anonymous)
    m = _mixin(path.entry)
    with owner_loop() as loop:
        started = time.monotonic()
        tools = on_loop(loop, m._probe_mcp(path.entry, timeout=BUDGET))
        elapsed = time.monotonic() - started
    assert [t.name for t in tools] == ["ping"]
    assert path.attempts == [None]
    assert not any(path.mcp.authorizations)
    assert (path.loads, path.locks, path.token_requests) == (0, 0, 0)
    assert elapsed < BUDGET


def test_the_credential_demand_is_noted_from_the_last_listing(path):
    path.connect()
    m = _mixin(path.entry)
    path.mcp.reset(mcp_fakes.bearer_only("at-old"))
    _probe(m, path.entry)
    assert path.entry.title in m._mcp_credential_demanded  # demanded, then bound with the credential
    path.mcp.reset(mcp_fakes.bearer_only("someone-else"))
    _record(m, path.entry)
    assert path.entry.title in m._mcp_credential_demanded  # demanded, and failed
    path.mcp.reset(mcp_fakes.anonymous)
    _probe(m, path.entry)
    assert path.entry.title not in m._mcp_credential_demanded  # listed without a credential: cleared


def test_a_refused_stored_token_is_force_refreshed_once_fenced_on_it(path, caplog):
    path.connect(access_token="at-revoked")
    path.token_endpoint()
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    m = _mixin(path.entry)
    with caplog.at_level(logging.WARNING), owner_loop() as loop:
        tools = on_loop(loop, m._probe_mcp(path.entry, timeout=BUDGET * 4))
    assert [t.name for t in tools] == ["ping"]
    assert path.attempts == [None, "Bearer at-revoked", "Bearer at-new"]
    assert path.token_requests == 1
    assert any(
        CONNECTION_ID in r.getMessage() and "refused the stored credential" in r.getMessage() for r in caplog.records
    )


def test_a_reconnect_required_connection_is_recorded_auth_naming_the_connection(path):
    path.connect(expires_in=-5, health=AuthHealthState.RECONNECT_REQUIRED)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    m = _mixin(path.entry)
    with owner_loop() as loop:
        try:
            on_loop(loop, m._probe_mcp(path.entry, timeout=BUDGET * 4))
        except Exception as exc:
            m._record_failed_mcp(path.entry, exc)
    record = m._failed_mcps[path.entry.title]
    assert record["category"] == "auth"
    assert CONNECTION_ID in record["message"]
    assert "reconnect" in record["message"]
    assert path.token_requests == 0


def test_a_token_endpoint_that_is_down_is_recorded_auth_and_the_health_follows_the_engine(path, monkeypatch):
    import tai42_skeleton.connectors.runtime.resolver as resolver_mod

    monkeypatch.setattr(resolver_mod, "TRANSIENT_BACKOFF_BASE_SECONDS", 0.0)
    path.connect(expires_in=-5)
    path.token_endpoint(status=503, body={"error": "temporarily_unavailable"})
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    m = _mixin(path.entry)
    with owner_loop() as loop:
        try:
            on_loop(loop, m._probe_mcp(path.entry, timeout=BUDGET * 4))
        except Exception as exc:
            m._record_failed_mcp(path.entry, exc)
    record = m._failed_mcps[path.entry.title]
    assert record["category"] == "auth"
    assert CONNECTION_ID in record["message"]
    assert path.stored().auth_health_state is AuthHealthState.REFRESH_FAILING


@pytest.mark.parametrize("server_policy", ["demands", "anonymous"])
def test_a_forged_entry_is_listed_with_no_credential_ever(path, caplog, server_policy):
    path.connect()
    forged = TaiMCPConfig(
        title="borrowed",
        config=MCPConfig(type="http", url=path.mcp.url.replace("127.0.0.1", "localhost")),
        managed=ConnectorRef(connection_id=CONNECTION_ID, provider_id="acme", sub_service="mail"),
    )
    path.mcp.reset(mcp_fakes.bearer_only("at-old") if server_policy == "demands" else mcp_fakes.anonymous)
    m = _mixin(forged)
    with caplog.at_level(logging.ERROR), owner_loop() as loop:
        try:
            on_loop(loop, m._probe_mcp(forged, timeout=BUDGET))
        except Exception as exc:
            m._record_failed_mcp(forged, exc)
    assert path.attempts == [None]
    assert not any(path.mcp.authorizations)
    assert (path.loads, path.locks, path.token_requests) == (0, 0, 0)
    if server_policy == "demands":
        assert (m._failed_mcps["borrowed"]["category"], m._failed_mcps["borrowed"]["http_status"]) == ("auth", 401)
        assert any(r.levelno == logging.ERROR and "'borrowed'" in r.getMessage() for r in caplog.records)
    else:
        assert "borrowed" not in m._failed_mcps


def test_a_scope_challenge_at_the_listing_names_the_scopes_and_writes_nothing(path):
    path.connect()
    path.mcp.reset(
        lambda token: mcp_fakes.UNAUTHORIZED if token is None else mcp_fakes.insufficient_scope("mail.admin")
    )
    m = _mixin(path.entry)
    record = _record(m, path.entry)
    assert record["category"] == "auth"
    assert record["http_status"] == 403
    assert "mail.admin" in record["message"]
    assert "reconnect" in record["message"]
    assert path.store.writes == 0
    assert path.stored().scopes_wanted == []


# -- the budget is kept and the rotation survives, on each door's real loop ------------------------


def _slow_rotation(path: CredentialPath) -> None:
    path.connect(expires_in=-5)
    path.token_endpoint(delay=BUDGET + 0.7)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))


def _door(m: _Mixin, door: str, config: TaiMCPConfig, loop: asyncio.AbstractEventLoop) -> Any:
    """Drive one listing of ``config`` through ``door``; return the door's outcome."""
    if door == "reload":
        return run_blocking(m._load_mcps)
    if door == "targeted reload":
        return m._reload_mcp(config.title)
    if door == "reload-failed":
        m._failed_mcps[config.title] = _failed_row()
        return m._reload_failed_mcps()
    return on_loop(loop, m._probe_and_apply_failed({config.title: config}))


def _bound(m: _Mixin, door: str, outcome: Any, title: str) -> bool:
    if door == "reload":
        successes, _failures = outcome
        return [cfg.title for cfg, _tools in successes] == [title]
    if door == "targeted reload":
        return outcome["status"] == "ok"
    results = outcome if door == "reload-failed" else outcome[0]
    return [r["status"] for r in results] == ["ok"]


@pytest.mark.parametrize("door", ["reload", "targeted reload", "reload-failed", "re-probe"])
def test_the_budget_is_kept_and_the_rotation_survives(path, monkeypatch, door):
    _slow_rotation(path)
    m = _mixin(path.entry)
    monkeypatch.setattr(lifecycle_module, "is_epoch_rebuild_in_progress", lambda: True)
    with owner_loop() as loop:
        if door == "re-probe":
            m._failed_mcps[path.entry.title] = _failed_row()
        started = time.monotonic()
        outcome = _door(m, door, path.entry, loop)
        elapsed = time.monotonic() - started
        assert not _bound(m, door, outcome, path.entry.title)
        assert elapsed < BUDGET + 0.5
        path.eventually_rotated()
        assert path.token_requests == 1
        roomy_budgets(monkeypatch)
        m._failed_mcps[path.entry.title] = _failed_row()
        assert _bound(m, door, _door(m, door, path.entry, loop), path.entry.title)


def test_the_boot_listing_makes_no_refresh_request(path):
    _slow_rotation(path)
    m = _mixin(path.entry)
    record = _record(m, path.entry)  # no refresh owner is bound at boot
    assert (record["category"], record["http_status"]) == ("auth", 401)
    assert path.attempts == [None]
    assert path.token_requests == 0
    assert path.locks == 0


@pytest.mark.parametrize("door", ["boot", "targeted reload", "reload-failed", "re-probe"])
def test_each_caller_binds_a_managed_entry(path, door):
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old"))
    m = _mixin(path.entry)
    with owner_loop() as loop:
        if door == "boot":
            successes, failures = run_blocking(m._load_mcps)
            assert failures == []
            assert [cfg.title for cfg, _tools in successes] == [path.entry.title]
        else:
            if door == "re-probe":
                m._failed_mcps[path.entry.title] = _failed_row()
            assert _bound(m, door, _door(m, door, path.entry, loop), path.entry.title)
            assert m._mcp_bound_tools[path.entry.title]


@pytest.mark.parametrize("door", ["targeted reload", "re-probe"])
@pytest.mark.parametrize("entry_kind", ["expired token", "mismatched entry"])
def test_an_anonymous_listing_applies_with_no_refresh_and_no_eviction_error(path, door, entry_kind):
    path.connect(expires_in=-5)
    path.token_endpoint(delay=30)
    path.mcp.reset(mcp_fakes.anonymous)
    entry = path.entry
    if entry_kind == "mismatched entry":
        entry = TaiMCPConfig(
            title="borrowed",
            config=MCPConfig(type="http", url=path.mcp.url.replace("127.0.0.1", "localhost")),
            managed=ConnectorRef(connection_id=CONNECTION_ID, provider_id="acme", sub_service="mail"),
        )
    m = _mixin(entry)
    with owner_loop() as loop:
        m._failed_mcps[entry.title] = _failed_row()
        started = time.monotonic()
        assert _bound(m, door, _door(m, door, entry, loop), entry.title)
        assert time.monotonic() - started < BUDGET
    assert path.token_requests == 0
    assert path.locks == 0


def test_the_note_is_kept_beside_the_failed_set_of_the_generation(path):
    m = _mixin(path.entry)
    m._note_credential_demand("a", demanded=True)
    assert m._serving_core._mcp_credential_demanded == {"a"}
    m._note_credential_demand("a", demanded=False)
    assert m._serving_core._mcp_credential_demanded == set()
    assert threading.current_thread() is threading.main_thread()
