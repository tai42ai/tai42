"""The connection doors for a discovered provider, against a loopback protocol fixture that records every request.

Authorize start, code exchange (with the redirect issuer check), the grant on the record, reconnect
inheritance, disconnect and the orphan revoke; the issuer pin and the URL guard at each door.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.errors import ConnectorClientRequiredError
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_contract.connectors.service import AliasInUseError, FlowOperation, StartConnectResult
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import authorization, discovery
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth.authorization import OAuthAuthorizationCancelledError, OAuthIssuerMismatchError
from tai42_skeleton.connectors.oauth.client import RedirectUriNotAllowedError
from tai42_skeleton.connectors.oauth.discovery import OAuthDiscoveryError
from tai42_skeleton.connectors.oauth.state import OAuthFlowState
from tai42_skeleton.connectors.service.connection_service import (
    complete_connect,
    disconnect,
    start_connect,
)

from ..conftest import CID, make_discovered_descriptor, make_oauth_descriptor
from ..oauth_fakes import FakeOAuthServer, Reply, serve
from .conftest import ORIGIN, REDIRECT

SECRET = "remote-env-secret"


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    monkeypatch.setenv("REMOTE_CLIENT_ID", "remote-env-id")
    monkeypatch.setenv("REMOTE_CLIENT_SECRET", SECRET)
    reset_all_settings()


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    discovery.reset_discovery_cache()
    yield
    discovery.reset_discovery_cache()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


def _publish(fake: FakeOAuthServer, servers: tuple[str, ...] = ("/a",), **metadata) -> str:
    """An MCP server at ``/mcp`` listing ``servers``, each with a token and a revocation endpoint."""
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm", scope="files.read")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}{p}" for p in servers]})
    for path in servers:
        fake.authorization_server(
            path,
            **{
                "revocation_endpoint": f"{fake.base}{path}/revoke",
                "scopes_supported": ["files.read", "offline_access"],
                **metadata,
            },
        )
    return mcp


async def _start(**kwargs) -> StartConnectResult:
    result = await start_connect(**kwargs)
    assert isinstance(result, StartConnectResult)
    return result


def _no_secret_sent(fake: FakeOAuthServer) -> None:
    for request in fake.received:
        assert SECRET.encode() not in request.body
        assert all(SECRET not in value for value in request.headers.values())


def _token_and_revoke_requests(fake: FakeOAuthServer) -> list[str]:
    return [r.path for r in fake.received if r.path.endswith(("/token", "/revoke"))]


def _flow(descriptor_id: str, *, issuer: str | None, resource: str | None, **overrides) -> OAuthFlowState:
    base = {
        "flow_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
        "provider_id": descriptor_id,
        "alias": "team",
        "requested_scopes": ["files.read"],
        "enabled_sub_services": ["files"],
        "pkce_verifier": "verifier",
        "return_url": "/connectors",
        "redirect_uri": REDIRECT,
        "operation": FlowOperation.CONNECT,
        "issuer": issuer,
        "resource": resource,
    }
    base.update(overrides)
    return OAuthFlowState(**base)


def _record(*, grant: ConnectionGrant | None, refresh_token: str | None = "old-rt", provider_id: str = "remote"):
    now = datetime.now(UTC)
    return ConnectionRecord(
        connection_id=CID,
        provider_id=provider_id,
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=["files"],
        granted_scopes=["files.read"],
        scopes_wanted=["files.write"],
        access_token=SecretStr("old-at"),
        refresh_token=SecretStr(refresh_token) if refresh_token else None,
        auth_health_state=AuthHealthState.HEALTHY,
        grant=grant,
        created_at=now,
    )


# -- authorize start ------------------------------------------------------------------


async def test_start_records_the_discovered_facts_and_builds_the_url_from_them(harness, fake: FakeOAuthServer):
    _, _, _, flows, _, providers = harness
    mcp = _publish(fake, authorization_response_iss_parameter_supported=True)
    issuer = f"{fake.base}/a"
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=issuer)
    result = await _start(
        provider_id="remote",
        alias="team",
        enabled_sub_services=["files"],
        return_url="/connectors",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    flow = flows[result.flow_id]
    assert flow.issuer == issuer
    assert flow.resource == mcp
    assert flow.client_id is None
    assert flow.iss_required
    assert flow.requested_scopes == ["files.read", "offline_access"]
    url = urlparse(result.authorize_url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{issuer}/authorize"
    query = parse_qs(url.query)
    assert query["resource"] == [mcp]
    assert query["client_id"] == ["remote-env-id"]
    assert query["scope"] == ["files.read offline_access"]
    _no_secret_sent(fake)


async def test_an_env_client_flow_keeps_the_callers_redirect_uri(harness, fake: FakeOAuthServer, monkeypatch):
    _, _, _, flows, _, providers = harness
    monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", "https://app.example.com,https://bridge.example.com")
    reset_all_settings()
    mcp = _publish(fake)
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a")
    bridge = "https://bridge.example.com/oauth-bridge.html"
    result = await _start(
        provider_id="remote",
        alias="team",
        enabled_sub_services=["files"],
        return_url="/x",
        redirect_uri=bridge,
        origin=ORIGIN,
    )
    assert flows[result.flow_id].redirect_uri == bridge


async def test_a_stored_client_flow_whose_callback_is_refused_stops_before_discovery_and_storage(
    harness, fake: FakeOAuthServer, monkeypatch
):
    _, _, _, flows, events, providers = harness
    monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", "http://intranet.example.com")
    reset_all_settings()
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    with pytest.raises(RedirectUriNotAllowedError):
        await start_connect(
            provider_id="remote",
            alias="team",
            enabled_sub_services=["files"],
            return_url="/x",
            # A bridge the caller computed is never used for a stored-client provider.
            redirect_uri="https://bridge.example.com/oauth-bridge.html",
            origin="http://intranet.example.com",
        )
    assert fake.received == []
    assert flows == {}
    assert events["state_put"] == []


async def test_a_stored_client_provider_needs_a_client_until_one_is_set(harness, fake: FakeOAuthServer):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    with pytest.raises(ConnectorClientRequiredError):
        await start_connect(
            provider_id="remote",
            alias="team",
            enabled_sub_services=["files"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )
    assert flows == {}


async def test_a_pinned_issuer_the_server_does_not_list_fails_the_start_and_sends_nothing(
    harness, fake: FakeOAuthServer
):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake), issuer=f"{fake.base}/b")
    with pytest.raises(OAuthDiscoveryError) as info:
        await start_connect(
            provider_id="remote",
            alias="team",
            enabled_sub_services=["files"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )
    assert info.value.step == "issuer-mismatch"
    assert flows == {}
    assert _token_and_revoke_requests(fake) == []
    _no_secret_sent(fake)


# -- code exchange ------------------------------------------------------------------


async def _started(harness, fake: FakeOAuthServer, **publish) -> tuple[str, str]:
    """Start a pinned env-client flow; returns ``(flow_id, issuer)``."""
    _, _, _, _, _, providers = harness
    mcp = _publish(fake, **publish)
    issuer = f"{fake.base}/a"
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=issuer)
    result = await _start(
        provider_id="remote",
        alias="team",
        enabled_sub_services=["files"],
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    return result.flow_id, issuer


async def test_complete_exchanges_the_code_at_the_logins_server_with_its_resource(harness, fake: FakeOAuthServer):
    flow_id, issuer = await _started(harness, fake)
    fake.route("POST", "/a/token", Reply(body={"access_token": "new-at", "scope": "files.read"}))
    result = await complete_connect(flow_id=flow_id, code="the-code", iss=issuer)
    assert result.connection_id
    (token_request,) = fake.requests_to("/a/token")
    form = token_request.form
    assert form["code"] == "the-code"
    assert form["resource"] == f"{fake.base}/mcp"
    assert form["client_id"] == "remote-env-id"
    assert form["client_secret"] == SECRET


async def test_complete_persists_the_grant_on_the_record(harness, fake: FakeOAuthServer, monkeypatch):
    persisted: list[ConnectionRecord] = []

    async def _capture(record, **kwargs):
        persisted.append(record.model_copy())

    import tai42_skeleton.connectors.service.connection_service.complete as complete_mod

    monkeypatch.setattr(complete_mod, "_persist", _capture)
    flow_id, issuer = await _started(harness, fake)
    fake.route("POST", "/a/token", Reply(body={"access_token": "new-at"}))
    await complete_connect(flow_id=flow_id, code="c", iss=issuer)
    (record,) = persisted
    assert record.grant == ConnectionGrant(issuer=issuer, resource=f"{fake.base}/mcp", client_id=None)
    assert record.refresh_token is None
    assert record.access_token_expires_at is None


@pytest.mark.parametrize(
    ("supported", "iss_suffix", "accepted"),
    [
        (True, "", True),
        (True, "/other", False),
        (True, None, False),
        (False, "/other", False),
        (False, "", True),
        (False, None, True),
    ],
)
async def test_the_redirect_issuer_table_decides_before_any_token_request(
    harness, fake: FakeOAuthServer, supported: bool, iss_suffix: str | None, accepted: bool
):
    flow_id, issuer = await _started(harness, fake, authorization_response_iss_parameter_supported=supported)
    fake.route("POST", "/a/token", Reply(body={"access_token": "new-at", "refresh_token": "rt"}))
    iss = None if iss_suffix is None else f"{issuer}{iss_suffix}"
    if accepted:
        await complete_connect(flow_id=flow_id, code="c", iss=iss)
        assert len(fake.requests_to("/a/token")) == 1
    else:
        with pytest.raises(OAuthIssuerMismatchError):
            await complete_connect(flow_id=flow_id, code="c", iss=iss)
        assert fake.requests_to("/a/token") == []


@pytest.mark.parametrize("variant", ["trailing-slash", "case"])
async def test_an_iss_that_differs_in_any_way_is_rejected(harness, fake: FakeOAuthServer, variant: str):
    # A default port is covered on the comparison itself (the fixture listens on a random port).
    flow_id, issuer = await _started(harness, fake, authorization_response_iss_parameter_supported=True)
    iss = {"trailing-slash": f"{issuer}/", "case": issuer.replace("http://", "HTTP://")}[variant]
    with pytest.raises(OAuthIssuerMismatchError):
        await complete_connect(flow_id=flow_id, code="c", iss=iss)
    assert fake.requests_to("/a/token") == []


async def test_an_error_answer_with_a_wrong_iss_is_an_issuer_mismatch_and_the_flow_is_spent(
    harness, fake: FakeOAuthServer
):
    _, _, _, flows, _, _ = harness
    flow_id, issuer = await _started(harness, fake, authorization_response_iss_parameter_supported=True)
    with pytest.raises(OAuthIssuerMismatchError) as info:
        await complete_connect(flow_id=flow_id, code=None, iss=f"{issuer}/x", error="access_denied")
    assert "access_denied" not in str(info.value)
    assert flow_id not in flows


async def test_an_error_answer_with_the_right_iss_is_cancelled(harness, fake: FakeOAuthServer):
    flow_id, issuer = await _started(harness, fake, authorization_response_iss_parameter_supported=True)
    with pytest.raises(OAuthAuthorizationCancelledError):
        await complete_connect(flow_id=flow_id, code=None, iss=issuer, error="access_denied")
    assert fake.requests_to("/a/token") == []


@pytest.mark.parametrize("iss", [None, "https://anything.example.com"])
async def test_a_declared_flow_completes_with_and_without_an_iss(harness, monkeypatch, iss):
    _, _, _, flows, _, _ = harness
    flow = _flow("acme", issuer=None, resource=None, enabled_sub_services=["mail"])
    flows[flow.flow_id] = flow
    seen: list[dict] = []

    async def _exchange(**kwargs):
        seen.append(kwargs)
        from .conftest import _token_response

        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", _exchange)
    result = await complete_connect(flow_id=flow.flow_id, code="c", iss=iss)
    assert result.connection_id
    assert seen[0]["require_refresh_token"] is True
    assert seen[0]["resource"] is None


async def test_an_issuer_change_mid_login_fails_before_any_token_request(harness, fake: FakeOAuthServer):
    _, _, _, _, _, providers = harness
    flow_id, _issuer = await _started(harness, fake)
    # The descriptor's pin now names another server than the login's.
    providers["remote"] = make_discovered_descriptor(mcp_url=f"{fake.base}/mcp", issuer=f"{fake.base}/b")
    with pytest.raises(oauth_client.OAuthError):
        await complete_connect(flow_id=flow_id, code="c")
    assert _token_and_revoke_requests(fake) == []
    _no_secret_sent(fake)


async def test_a_flow_started_declared_and_completed_discovered_fails_cleanly(harness, fake: FakeOAuthServer):
    _, _, _, flows, _, providers = harness
    mcp = _publish(fake)
    flow = _flow("remote", issuer=None, resource=None)
    flows[flow.flow_id] = flow
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a")
    with pytest.raises(oauth_client.OAuthError, match="now resolves to issuer"):
        await complete_connect(flow_id=flow.flow_id, code="c")
    assert _token_and_revoke_requests(fake) == []


async def test_a_completion_whose_resource_changed_fails_before_any_token_request(harness, fake: FakeOAuthServer):
    _, _, _, flows, _, providers = harness
    mcp = _publish(fake)
    issuer = f"{fake.base}/a"
    flow = _flow("remote", issuer=issuer, resource="https://elsewhere.example.com/mcp")
    flows[flow.flow_id] = flow
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=issuer)
    with pytest.raises(oauth_client.OAuthError, match="resource"):
        await complete_connect(flow_id=flow.flow_id, code="c", iss=issuer)
    assert _token_and_revoke_requests(fake) == []


async def test_a_token_endpoint_the_guard_refuses_fails_the_exchange(harness, fake: FakeOAuthServer):
    flow_id, issuer = await _started(harness, fake, token_endpoint="https://10.9.9.9/token")
    with pytest.raises(oauth_client.CodeExchangeFailedError, match="refused"):
        await complete_connect(flow_id=flow_id, code="c", iss=issuer)


# -- the orphan revoke ----------------------------------------------------------------


async def test_the_orphan_revoke_goes_to_the_logins_server_with_the_logins_client(
    harness, fake: FakeOAuthServer, monkeypatch
):
    _, _, store, flows, _, providers = harness
    mcp = _publish(fake, servers=("/b", "/a"))
    server_a = f"{fake.base}/a"
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp)
    flow = _flow("remote", issuer=server_a, resource=mcp, client_id="doc-1")
    flows[flow.flow_id] = flow
    document_client = authorization.ResolvedClient(
        client_id="doc-1", client_secret=None, auth_method="none", source="document"
    )

    async def _resolve_client(descriptor, resolved, *, redirect_uri=None, client_id=None):
        assert client_id == "doc-1"
        return document_client

    monkeypatch.setattr(authorization, "resolve_client", _resolve_client)
    fake.route("POST", "/a/token", Reply(body={"access_token": "orphan-at"}))
    fake.route("POST", "/a/revoke", Reply(status=200))
    store.aliases[("remote", "team")] = "someone-else"
    with pytest.raises(AliasInUseError):
        await complete_connect(flow_id=flow.flow_id, code="c")
    (revoked,) = fake.requests_to("/a/revoke")
    assert revoked.form == {"token": "orphan-at", "client_id": "doc-1"}
    assert fake.requests_to("/b/revoke") == []


# -- reconnect inheritance -----------------------------------------------------------


async def _reconnect(harness, fake: FakeOAuthServer, record_grant: ConnectionGrant | None, monkeypatch):
    cs_mod, records, _, flows, _, providers = harness
    mcp = _publish(fake)
    issuer = f"{fake.base}/a"
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=issuer)
    records[CID] = _record(grant=record_grant)
    flow = _flow("remote", issuer=issuer, resource=mcp, operation=FlowOperation.RECONNECT, reconnect_connection_id=CID)
    flows[flow.flow_id] = flow
    persisted: list[ConnectionRecord] = []

    async def _capture(record, **kwargs):
        persisted.append(record.model_copy())

    import tai42_skeleton.connectors.service.connection_service.complete as complete_mod

    monkeypatch.setattr(complete_mod, "_persist", _capture)

    async def _no_cooldown(cid):
        return None

    monkeypatch.setattr(cs_mod, "clear_refresh_cooldown", _no_cooldown)
    fake.route("POST", "/a/token", Reply(body={"access_token": "new-at"}))
    await complete_connect(flow_id=flow.flow_id, code="c", iss=issuer)
    return persisted[0], issuer


async def test_a_reconnect_with_the_same_grant_keeps_the_old_refresh_token(harness, fake: FakeOAuthServer, monkeypatch):
    grant = ConnectionGrant(issuer=f"{fake.base}/a", resource=f"{fake.base}/mcp")
    record, _ = await _reconnect(harness, fake, grant, monkeypatch)
    assert record.refresh_token is not None
    assert record.refresh_token.get_secret_value() == "old-rt"
    assert record.access_token_expires_at is not None  # the inherited refresh token makes the default life apply
    assert record.scopes_wanted == []


@pytest.mark.parametrize(
    "old_grant",
    [ConnectionGrant(issuer="https://other.example.com"), ConnectionGrant(issuer="SELF", client_id="stored-1"), None],
)
async def test_a_reconnect_under_another_grant_does_not_inherit(harness, fake: FakeOAuthServer, monkeypatch, old_grant):
    if old_grant is not None and old_grant.issuer == "SELF":
        old_grant = ConnectionGrant(issuer=f"{fake.base}/a", client_id="stored-1")
    record, issuer = await _reconnect(harness, fake, old_grant, monkeypatch)
    assert record.refresh_token is None
    assert record.access_token_expires_at is None
    assert record.grant is not None
    assert record.grant.issuer == issuer


# -- disconnect ----------------------------------------------------------------------


async def test_disconnect_of_a_record_with_no_refresh_token_revokes_the_access_token(harness, monkeypatch):
    _, records, store, _, _, _ = harness
    records[CID] = _record(grant=None, refresh_token=None, provider_id="acme")
    revoked: list[str] = []

    async def _revoke(*, descriptor, resolved, client, token):
        revoked.append(token)
        return oauth_client.RevokeOutcome(outcome="success", http_status=200)

    monkeypatch.setattr(oauth_client, "revoke", _revoke)
    result = await disconnect(connection_id=CID)
    assert revoked == ["old-at"]
    assert result.upstream_revoke_outcome == "success"
    assert CID in store.deleted


async def test_disconnect_when_discovery_fails_removes_the_connection(harness, fake: FakeOAuthServer):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=f"{fake.base}/mcp", issuer=f"{fake.base}/a")
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert CID in store.deleted


async def test_disconnect_when_the_client_is_missing_removes_the_connection(harness, fake: FakeOAuthServer):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="row-gone"))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert _token_and_revoke_requests(fake) == []
    assert CID in store.deleted


async def test_disconnect_when_the_server_no_longer_lists_the_grants_issuer_sends_nothing(
    harness, fake: FakeOAuthServer
):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake, servers=("/b",)))
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert _token_and_revoke_requests(fake) == []
    assert CID in store.deleted


async def test_disconnect_when_the_pin_names_another_server_sends_nothing(harness, fake: FakeOAuthServer):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake), issuer=f"{fake.base}/b")
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert _token_and_revoke_requests(fake) == []
    _no_secret_sent(fake)
    assert CID in store.deleted


async def test_disconnect_revokes_at_the_grants_server_with_the_env_client(harness, fake: FakeOAuthServer):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake), issuer=f"{fake.base}/a")
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"))
    fake.route("POST", "/a/revoke", Reply(status=200))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "success"
    (revoked,) = fake.requests_to("/a/revoke")
    assert revoked.form == {"token": "old-rt", "client_id": "remote-env-id", "client_secret": SECRET}
    assert CID in store.deleted


async def test_disconnect_at_a_revocation_endpoint_the_guard_refuses(harness, fake: FakeOAuthServer):
    _, records, store, _, _, providers = harness
    mcp = _publish(fake, revocation_endpoint="https://172.16.5.5/revoke")
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a")
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert CID in store.deleted


async def test_disconnect_of_a_declared_record_needs_no_client(harness, monkeypatch):
    # A declared provider's revoke sends only the token, so an unset env client does not stop it.
    _, records, _, _, _, providers = harness
    monkeypatch.delenv("ACME_CLIENT_ID")
    providers["acme"] = make_oauth_descriptor()
    records[CID] = _record(grant=None, provider_id="acme")
    seen: list = []

    async def _revoke(*, descriptor, resolved, client, token):
        seen.append(client)
        return oauth_client.RevokeOutcome(outcome="success", http_status=200)

    monkeypatch.setattr(oauth_client, "revoke", _revoke)
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "success"
    assert seen == [None]
