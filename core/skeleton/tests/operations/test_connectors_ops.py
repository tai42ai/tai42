"""Op-level oracles for the connector operations.

The route oracles in ``tests/routers/test_connectors.py`` drive the reads and the
happy/error paths of the mutations through the adapter; these pin the branches the
round-trip route tests do not reach (the reconnect/patch unknown-connection 404s)
and the destructive projection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import get_args

import pytest
from tai42_contract.connectors.errors import OperatorMisconfiguredError
from tai42_contract.connectors.models import AuthHealthState
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.connectors.runtime.resolver import ConnectorReconnectRequiredError, ManagedAuth
from tai42_skeleton.connectors.store.catalog_store import ConnectorCategory
from tai42_skeleton.operations import (
    BadRequestError,
    NotFoundError,
    NotSupportedError,
    OperationRegistry,
    operation_metadata_of,
)
from tai42_skeleton.operations import connectors as conn_ops
from tai42_skeleton.operations.projection import project_operations
from tests.connectors.conftest import (
    CID,
    CID2,
    make_noauth_record,
    make_noauth_stdio_descriptor,
    make_oauth_descriptor,
    make_oauth_record,
)


@pytest.fixture(autouse=True)
def _connector_store_configured(monkeypatch):
    # the connector surface answers OFF with no store configured. These tests
    # exercise the ON feature (a fake DB stands in), so satisfy the presence gate.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "x")


def _missing_loader():
    async def _load(cid):
        return None

    return _load


def _record(cid="c1"):
    return SimpleNamespace(
        connection_id=cid,
        provider_id="github",
        alias="work",
        kind="oauth",
        account_identity="me@x",
        enabled_sub_services=["repo"],
        granted_scopes=["repo"],
        auth_health_state=AuthHealthState.HEALTHY,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_get_connection_returns_secret_free_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _load(cid):
        return _record(cid)

    async def _no_probe(record):
        return []

    monkeypatch.setattr(conn_ops, "load_record_or_none", _load)
    # This test pins the secret-free view; the live reachability probe is exercised
    # by the N1 tests below.
    monkeypatch.setattr(conn_ops, "_probe_unreachable", _no_probe)
    view = await conn_ops.get_connection(connection_id="c1")
    assert view["connection_id"] == "c1"
    assert "access_token" not in view
    assert "config_values" not in view


async def test_reconnect_returns_authorize_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _load(cid):
        return _record(cid)

    async def _reconnect(**kw):
        return SimpleNamespace(flow_id="f1", authorize_url="https://gh/auth")

    monkeypatch.setattr(conn_ops, "load_record_or_none", _load)
    monkeypatch.setattr(conn_ops.connection_service, "start_reconnect", _reconnect)
    result = await conn_ops.reconnect(
        connection_id="c1",
        enabled_sub_services=["repo"],
        return_url="/connectors",
        redirect_uri="https://app/oauth-bridge.html",
        origin="https://app",
    )
    assert result == {"flow_id": "f1", "authorize_url": "https://gh/auth"}


async def test_patch_sub_services_returns_result_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _load(cid):
        return _record(cid)

    async def _patch(**kw):
        return SimpleNamespace(
            connection_id="c1",
            enabled_sub_services=["repo", "issues"],
            consent_required=False,
            flow_id=None,
            authorize_url=None,
            added_manifest_entries=["issues"],
            removed_manifest_entries=[],
            fanout={"mode": "local-only"},
        )

    monkeypatch.setattr(conn_ops, "load_record_or_none", _load)
    monkeypatch.setattr(conn_ops.connection_service, "patch_sub_services", _patch)
    result = await conn_ops.patch_sub_services(
        connection_id="c1",
        enabled_sub_services=["repo", "issues"],
        return_url="/connectors",
        redirect_uri="https://app/oauth-bridge.html",
        origin="https://app",
    )
    assert result["connection_id"] == "c1"
    assert result["enabled_sub_services"] == ["repo", "issues"]
    assert result["consent_required"] is False
    # The patch that mutated the manifest threads the service's fleet report through.
    assert result["fanout"] == {"mode": "local-only"}


# -- OFF state: no connector store configured --------------------------


def _off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the OFF gate: drop the feature's own password AND the shared default so
    # the fresh-read presence probe answers unconfigured (overrides the autouse ON).
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)


async def test_list_connections_off_answers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store the read degrades to the honest empty collection, no store touched.
    _off(monkeypatch)
    assert await conn_ops.list_connections() == {"items": [], "total": 0, "unhealthy": 0}


async def test_get_connection_off_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store no connection can exist — the miss is byte-identical to a genuine 404.
    _off(monkeypatch)
    with pytest.raises(NotFoundError, match="connection not found"):
        await conn_ops.get_connection(connection_id="c1")


async def test_start_connect_off_refuses_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Opening a flow needs the store to persist the connection — refuse with a named,
    # machine-readable reason rather than reaching for an absent store.
    _off(monkeypatch)
    with pytest.raises(NotSupportedError) as exc_info:
        await conn_ops.start_connect(
            provider_id="github",
            alias="work",
            enabled_sub_services=["repo"],
            config_values={},
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )
    assert exc_info.value.extra["code"] == "connectors-not-configured"


async def test_disconnect_off_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store no connection can exist — a 404 byte-identical to a genuine miss.
    _off(monkeypatch)
    with pytest.raises(NotFoundError, match="connection not found"):
        await conn_ops.disconnect(connection_id="c1")


async def test_reconnect_off_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _off(monkeypatch)
    with pytest.raises(NotFoundError, match="connection not found"):
        await conn_ops.reconnect(
            connection_id="c1",
            enabled_sub_services=["repo"],
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )


async def test_patch_sub_services_off_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _off(monkeypatch)
    with pytest.raises(NotFoundError, match="connection not found"):
        await conn_ops.patch_sub_services(
            connection_id="c1",
            enabled_sub_services=["repo"],
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )


async def test_reconnect_unknown_connection_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn_ops, "load_record_or_none", _missing_loader())
    with pytest.raises(NotFoundError, match="connection not found"):
        await conn_ops.reconnect(
            connection_id="gone",
            enabled_sub_services=["repo"],
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )


async def test_patch_sub_services_unknown_connection_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn_ops, "load_record_or_none", _missing_loader())
    with pytest.raises(NotFoundError, match="connection not found"):
        await conn_ops.patch_sub_services(
            connection_id="gone",
            enabled_sub_services=["repo"],
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )


def test_connector_reads_project_and_mutations_carry_destructive_hint() -> None:
    # The reads (providers/connections/get_connection) are default-in; the mutations
    # are destructive (off the default surface, includable) and carry destructiveHint.
    reg = OperationRegistry()
    for op in (
        conn_ops.list_connector_providers,
        conn_ops.list_connections,
        conn_ops.get_connection,
        conn_ops.start_connect,
        conn_ops.disconnect,
        conn_ops.reconnect,
        conn_ops.patch_sub_services,
    ):
        reg.register(operation_metadata_of(op))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    class _App:
        def __init__(self) -> None:
            self.tools = _Rec()

    app = _App()
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    assert {"list_connector_providers", "list_connections", "get_connection"} <= set(names)
    assert app.tools.registered["list_connector_providers"]["annotations"] is None
    # start_connect / reconnect / patch_sub_services declare destructive=True.
    assert app.tools.registered["start_connect"]["annotations"].destructiveHint is True
    assert app.tools.registered["reconnect"]["annotations"].destructiveHint is True
    assert app.tools.registered["patch_sub_services"]["annotations"].destructiveHint is True


# -- C1: missing OAuth provider creds → named actionable 501 -----------------


async def test_start_connect_missing_provider_creds_is_501(monkeypatch: pytest.MonkeyPatch) -> None:
    # A provider registered but missing its operator-supplied OAuth client credentials
    # env var is a capability the deployment does not provide — a named 501 carrying a
    # machine-readable code + the env-var pointer, NOT an unnamed 500 and NOT a 503.
    async def _start(**kw):
        raise OperatorMisconfiguredError(env_var="CONNECTORS_ACME_CLIENT_ID", provider_id="acme")

    monkeypatch.setattr(conn_ops.connection_service, "start_connect", _start)
    with pytest.raises(NotSupportedError) as exc_info:
        await conn_ops.start_connect(
            provider_id="acme",
            alias="work",
            enabled_sub_services=["mail"],
            config_values={},
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )
    assert exc_info.value.status == 501
    assert exc_info.value.extra["code"] == "connector-provider-not-configured"
    assert exc_info.value.extra["env_var"] == "CONNECTORS_ACME_CLIENT_ID"
    # The actionable message names the env var so an operator knows exactly what to set.
    assert "CONNECTORS_ACME_CLIENT_ID" in exc_info.value.message


async def test_reconnect_missing_provider_creds_is_501(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({"c1": _record("c1")}))

    async def _reconnect(**kw):
        raise OperatorMisconfiguredError(env_var="CONNECTORS_ACME_CLIENT_SECRET", provider_id="acme")

    monkeypatch.setattr(conn_ops.connection_service, "start_reconnect", _reconnect)
    with pytest.raises(NotSupportedError) as exc_info:
        await conn_ops.reconnect(
            connection_id="c1",
            enabled_sub_services=["mail"],
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )
    assert exc_info.value.status == 501
    assert exc_info.value.extra["env_var"] == "CONNECTORS_ACME_CLIENT_SECRET"


async def test_patch_sub_services_missing_provider_creds_is_501(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({"c1": _record("c1")}))

    async def _patch(**kw):
        raise OperatorMisconfiguredError(env_var="CONNECTORS_ACME_CLIENT_ID", provider_id="acme")

    monkeypatch.setattr(conn_ops.connection_service, "patch_sub_services", _patch)
    with pytest.raises(NotSupportedError) as exc_info:
        await conn_ops.patch_sub_services(
            connection_id="c1",
            enabled_sub_services=["mail"],
            return_url="/connectors",
            redirect_uri="https://app/oauth-bridge.html",
            origin="https://app",
        )
    assert exc_info.value.status == 501
    assert exc_info.value.extra["code"] == "connector-provider-not-configured"


def test_reconnect_and_patch_declare_not_supported_error() -> None:
    # The 501 is a declared error class on both ops so the route's error-status surface
    # (and the projected tool) advertise it, not just the runtime raise.
    assert NotSupportedError in operation_metadata_of(conn_ops.reconnect).error_classes
    assert NotSupportedError in operation_metadata_of(conn_ops.patch_sub_services).error_classes


# -- N1: single-connection reachability probe --------------------------------


def _acall(mapping):
    async def _f(cid):
        return mapping.get(cid)

    return _f


async def test_get_connection_probes_reachability(monkeypatch: pytest.MonkeyPatch) -> None:
    # The single-connection GET probes every enabled sub-service and returns the
    # ones that did not answer on ``unreachable_sub_services``.
    record = make_oauth_record(provider_id="acme", enabled_sub_services=["mail", "cal"])
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_oauth_descriptor(provider_id="acme"))

    async def _resolve(cid, pid, sub, *, allow_refresh):
        assert allow_refresh is False  # the idempotent GET resolves read-only
        return ManagedAuth(access_token="tok")

    async def _probe(descriptor, sub_service, **kwargs):
        return sub_service == "mail"  # cal is down

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)

    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["cal"]


async def test_get_connection_provider_gone_all_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A connection whose provider plugin is no longer registered has nothing
    # reachable — every enabled sub-service is unreachable.
    record = make_oauth_record(provider_id="acme", enabled_sub_services=["mail", "cal"])
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))

    def _missing(pid):
        raise KeyError(pid)

    monkeypatch.setattr(conn_ops, "get_provider", _missing)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["mail", "cal"]


async def test_get_connection_noauth_probes_with_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # A no-auth connection probes with its decrypted client config on the transport
    # channel and never resolves an OAuth token.
    record = make_noauth_record(provider_id="widgets", enabled_sub_services=["search"], config_values={"api_key": "k"})
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_noauth_stdio_descriptor(provider_id="widgets"))
    captured: dict = {}

    async def _probe(descriptor, sub_service, **kwargs):
        captured["config_values"] = kwargs.get("config_values")
        return True

    monkeypatch.setattr(conn_ops, "probe", _probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == []
    assert captured["config_values"] == {"api_key": "k"}


async def test_get_connection_oauth_unresolvable_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # An OAuth connection whose token cannot be resolved (reconnect required) has no
    # working sub-service — it reads as unreachable, and the probe is never dispatched.
    record = make_oauth_record(provider_id="acme", enabled_sub_services=["mail"])
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_oauth_descriptor(provider_id="acme"))

    async def _resolve(cid, pid, sub, *, allow_refresh):
        raise ConnectorReconnectRequiredError("reconnect", connection_id=cid)

    async def _probe(*args, **kwargs):
        raise AssertionError("probe must not run when the token cannot be resolved")

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["mail"]


async def test_get_connection_stale_token_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The idempotent GET resolves the OAuth credential READ-ONLY: a stale token
    # yields no credential (None), so the sub-service reads unreachable and the probe
    # is never dispatched — the read never drives a refresh.
    record = make_oauth_record(provider_id="acme", enabled_sub_services=["mail"])
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_oauth_descriptor(provider_id="acme"))

    async def _resolve(cid, pid, sub, *, allow_refresh):
        assert allow_refresh is False
        return None

    async def _probe(*args, **kwargs):
        raise AssertionError("probe must not run when there is no credential")

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["mail"]


async def test_get_connection_missing_creds_is_unreachable_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    # An OAuth connection whose credential read hits a missing provider-credential
    # gap (OperatorMisconfiguredError, a RuntimeError) reads unreachable — a 200 with
    # the sub-service in unreachable_sub_services, NEVER the unnamed 500 the read door
    # must not emit. (Guards the C1 failure mode from reappearing on the N1 read door.)
    record = make_oauth_record(provider_id="acme", enabled_sub_services=["mail"])
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_oauth_descriptor(provider_id="acme"))

    async def _resolve(cid, pid, sub, *, allow_refresh):
        raise OperatorMisconfiguredError(env_var="ACME_CLIENT_ID", provider_id="acme")

    async def _probe(*args, **kwargs):
        raise AssertionError("probe must not run when the credential cannot be resolved")

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["mail"]


async def test_get_connection_probe_is_bounded_by_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole per-sub-service probe is bounded: a sub-service that hangs past the
    # timeout reads unreachable rather than blocking the idempotent GET.
    record = make_oauth_record(provider_id="acme", enabled_sub_services=["mail"])
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_oauth_descriptor(provider_id="acme"))
    monkeypatch.setattr(conn_ops, "_SUB_SERVICE_PROBE_TIMEOUT_SECONDS", 0.02)

    async def _resolve(cid, pid, sub, *, allow_refresh):
        return ManagedAuth(access_token="tok")

    async def _slow_probe(descriptor, sub_service, **kwargs):
        await asyncio.sleep(1.0)
        return True

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _slow_probe)
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    assert view["unreachable_sub_services"] == ["mail"]


async def test_get_connection_probes_sub_services_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    # The enabled sub-services are probed concurrently, so the aggregate wall time
    # stays near a SINGLE per-item delay rather than their sum. A sequential probe
    # of N sub-services each delayed ``d`` would take ~N*d; concurrency observes all
    # N probes in flight at once and completes in ~d. The reported unreachable set
    # preserves ``enabled_sub_services`` order.
    subs = ["mail", "cal", "docs", "chat", "drive"]
    record = make_oauth_record(provider_id="acme", enabled_sub_services=subs)
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({record.connection_id: record}))
    monkeypatch.setattr(conn_ops, "get_provider", lambda pid: make_oauth_descriptor(provider_id="acme"))

    delay = 0.05
    in_flight = 0
    max_in_flight = 0
    down = {"cal", "chat"}  # these read unreachable

    async def _resolve(cid, pid, sub, *, allow_refresh):
        assert allow_refresh is False
        return ManagedAuth(access_token="tok")

    async def _probe(descriptor, sub_service, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(delay)
        finally:
            in_flight -= 1
        return sub_service not in down

    monkeypatch.setattr(conn_ops, "resolve_managed_auth", _resolve)
    monkeypatch.setattr(conn_ops, "probe", _probe)

    loop = asyncio.get_running_loop()
    started = loop.time()
    view = await conn_ops.get_connection(connection_id=record.connection_id)
    elapsed = loop.time() - started

    # Order preserved: the down sub-services in enabled order.
    assert view["unreachable_sub_services"] == ["cal", "chat"]
    # All probes were in flight at once — only a concurrent implementation reaches
    # a max-in-flight equal to the number of sub-services.
    assert max_in_flight == len(subs)
    # And the wall time is bounded near a single delay, well under the sequential sum.
    assert elapsed < delay * len(subs)


async def test_list_connections_is_probe_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # The list never probes: every item carries an empty unreachable list.
    records = [make_oauth_record(connection_id=CID), make_oauth_record(connection_id=CID2)]
    _wire_list(monkeypatch, records)

    async def _probe(*args, **kwargs):
        raise AssertionError("the list must not probe")

    monkeypatch.setattr(conn_ops, "probe", _probe)
    out = await conn_ops.list_connections()
    assert all(item["unreachable_sub_services"] == [] for item in out["items"])


# -- N2: list health surface (filter / unhealthy count / limit guard) --------


def _wire_list(monkeypatch, records) -> None:
    async def _list():
        return [r.connection_id for r in records]

    monkeypatch.setattr(conn_ops, "token_store", lambda: SimpleNamespace(list=_list))
    monkeypatch.setattr(conn_ops, "load_record_or_none", _acall({r.connection_id: r for r in records}))


async def test_list_connections_counts_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        make_oauth_record(connection_id=CID, health=AuthHealthState.HEALTHY),
        make_oauth_record(connection_id=CID2, health=AuthHealthState.RECONNECT_REQUIRED),
    ]
    _wire_list(monkeypatch, records)
    out = await conn_ops.list_connections()
    assert out["total"] == 2
    assert out["unhealthy"] == 1


async def test_list_connections_health_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        make_oauth_record(connection_id=CID, health=AuthHealthState.HEALTHY),
        make_oauth_record(connection_id=CID2, health=AuthHealthState.RECONNECT_REQUIRED),
    ]
    _wire_list(monkeypatch, records)
    out = await conn_ops.list_connections(health="reconnect_required")
    assert [item["connection_id"] for item in out["items"]] == [CID2]
    # total counts the filter matches; unhealthy is the unfiltered badge.
    assert out["total"] == 1
    assert out["unhealthy"] == 1


async def test_list_connections_limit_caps_items_but_reports_full_total(monkeypatch: pytest.MonkeyPatch) -> None:
    cid3 = "33333333-3333-4333-8333-333333333333"
    records = [make_oauth_record(connection_id=cid) for cid in (CID, CID2, cid3)]
    _wire_list(monkeypatch, records)
    out = await conn_ops.list_connections(limit=2)
    assert len(out["items"]) == 2
    assert out["total"] == 3  # the full match count, so the caller sees there is more


async def test_list_connections_invalid_health_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BadRequestError, match="invalid health filter"):
        await conn_ops.list_connections(health="bogus")


def test_the_published_health_filter_is_the_state_vocabulary() -> None:
    # ``ConnectionHealthFilter`` is what the listing door publishes as the ``?health=`` value
    # set, while ``AuthHealthState`` is what the door parses — a value in one and not the other
    # is either an advertised filter the door 400s on or a served state no client is told about.
    assert set(get_args(conn_ops.ConnectionHealthFilter)) == {state.value for state in AuthHealthState}


async def test_list_connections_invalid_limit_is_400() -> None:
    with pytest.raises(BadRequestError, match="limit must be between"):
        await conn_ops.list_connections(limit=0)
    with pytest.raises(BadRequestError, match="limit must be between"):
        await conn_ops.list_connections(limit=10_000)


# -- N3: categories served on the providers listing --------------------------


async def test_list_connector_providers_serves_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn_ops, "list_providers", lambda: [make_oauth_descriptor(provider_id="acme")])

    async def _categories():
        return [ConnectorCategory(id="productivity", display_name="Productivity", sort_order=2)]

    monkeypatch.setattr(conn_ops, "fetch_categories", _categories)
    out = await conn_ops.list_connector_providers()
    assert [p["id"] for p in out["providers"]] == ["acme"]
    assert out["categories"] == [{"id": "productivity", "display_name": "Productivity", "sort_order": 2}]


async def test_list_connector_providers_off_serves_empty_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store the category read is skipped (empty groupings); providers still
    # list from the in-memory registry.
    _off(monkeypatch)
    monkeypatch.setattr(conn_ops, "list_providers", lambda: [make_oauth_descriptor(provider_id="acme")])

    async def _categories():
        raise AssertionError("categories must not be read when the store is unconfigured")

    monkeypatch.setattr(conn_ops, "fetch_categories", _categories)
    out = await conn_ops.list_connector_providers()
    assert out["categories"] == []
    assert [p["id"] for p in out["providers"]] == ["acme"]
