"""The connection doors of a provider that names no env client, against a loopback protocol fixture and the
in-memory client store: which client and which issuer a new login uses, the deployment's own callback under
a central bridge, a refused client at the code exchange, a client that disappears mid-login, and disconnect.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.errors import ConnectorClientRequiredError
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_contract.connectors.service import FlowOperation, StartConnectResult
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import discovery
from tai42_skeleton.connectors.oauth.discovery import OAuthDiscoveryError
from tai42_skeleton.connectors.service.connection_service import (
    complete_connect,
    disconnect,
    patch_sub_services,
    start_connect,
    start_reconnect,
)

from ..client_store_fake import FakeClientStore
from ..conftest import CID, make_discovered_descriptor
from ..oauth_fakes import FakeOAuthServer, Reply, serve

ORIGIN = "https://app.example.com"
CALLBACK = "https://app.example.com/oauth-bridge.html"
BRIDGE = "https://bridge.example.com"
BRIDGE_CALLBACK = "https://bridge.example.com/oauth-bridge.html"
DOCUMENT = "https://app.example.com/oauth/client-metadata.json"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", f"{ORIGIN},{BRIDGE}")
    monkeypatch.setenv("REMOTE_CLIENT_ID", "remote-env-id")
    monkeypatch.setenv("REMOTE_CLIENT_SECRET", "remote-env-secret")
    reset_all_settings()
    discovery.reset_discovery_cache()
    yield
    discovery.reset_discovery_cache()


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTORS_OAUTH_BRIDGE_URL", BRIDGE)
    reset_all_settings()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


def _publish(fake: FakeOAuthServer, servers: tuple[str, ...] = ("/a",), *, registration: tuple[str, ...] = (), **md):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}{p}" for p in servers]})
    for path in servers:
        extra = {"revocation_endpoint": f"{fake.base}{path}/revoke", **md}
        if path in registration:
            extra["registration_endpoint"] = f"{fake.base}{path}/register"
            fake.route(
                "POST",
                f"{path}/register",
                Reply(status=201, body={"client_id": f"dyn{path.replace('/', '-')}", "client_secret": "dyn-secret"}),
            )
        fake.authorization_server(path, **extra)
    return mcp


async def _start(**overrides) -> StartConnectResult:
    kwargs = {
        "provider_id": "remote",
        "alias": "team",
        "enabled_sub_services": ["files"],
        "return_url": "/connectors",
        "redirect_uri": BRIDGE_CALLBACK,
        "origin": ORIGIN,
    }
    kwargs.update(overrides)
    result = await start_connect(**kwargs)
    assert isinstance(result, StartConnectResult)
    return result


def _query(result: StartConnectResult) -> dict[str, list[str]]:
    return parse_qs(urlparse(result.authorize_url).query)


def _authorize_base(result: StartConnectResult) -> str:
    url = urlparse(result.authorize_url)
    return f"{url.scheme}://{url.netloc}{url.path}"


def _record(*, grant: ConnectionGrant | None, enabled: list[str] | None = None, granted: list[str] | None = None):
    return ConnectionRecord(
        connection_id=CID,
        provider_id="remote",
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=enabled or ["files"],
        granted_scopes=granted if granted is not None else ["files.read"],
        access_token=SecretStr("old-at"),
        refresh_token=SecretStr("old-rt"),
        auth_health_state=AuthHealthState.HEALTHY,
        grant=grant,
        created_at=datetime.now(UTC),
    )


# -- which client and which issuer a new login uses ---------------------------------------


async def test_a_manual_client_keeps_its_issuer_wherever_the_server_lists_it(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake, servers=("/b", "/a"), registration=("/b",)))
    issuer_a = f"{fake.base}/a"
    await client_store_fake.put_manual("remote", issuer_a, "app-a", "app-secret", "client_secret_post")
    result = await _start()
    assert _authorize_base(result) == f"{issuer_a}/authorize"
    assert _query(result)["client_id"] == ["app-a"]
    flow = flows[result.flow_id]
    assert (flow.issuer, flow.client_id) == (issuer_a, "app-a")
    assert fake.requests_to("/b/register") == []
    assert client_store_fake.rows[0].client_id == "app-a"
    assert len(client_store_fake.rows) == 1


async def test_a_manual_clients_issuer_no_longer_listed_fails_the_start(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake, servers=("/b",), registration=("/b",)))
    await client_store_fake.put_manual("remote", f"{fake.base}/a", "app-a", "app-secret", "client_secret_post")
    with pytest.raises(OAuthDiscoveryError) as info:
        await _start()
    assert info.value.step == "issuer-mismatch"
    assert flows == {}
    assert fake.requests_to("/b/register") == []


async def test_with_no_manual_client_a_new_login_goes_to_the_first_listed_server(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(
        mcp_url=_publish(fake, servers=("/b", "/a"), registration=("/b", "/a"))
    )
    result = await _start()
    assert _authorize_base(result) == f"{fake.base}/b/authorize"
    assert flows[result.flow_id].client_id == "dyn-b"
    (registration,) = fake.requests_to("/b/register", "POST")
    assert registration.json["redirect_uris"] == [CALLBACK]
    assert fake.requests_to("/a/register") == []


async def test_a_provider_with_no_way_to_get_a_client_needs_one_until_one_is_set(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    with pytest.raises(ConnectorClientRequiredError):
        await _start()
    assert flows == {}
    await client_store_fake.put_manual("remote", f"{fake.base}/a", "app-a", None, "none")
    result = await _start()
    assert _query(result)["client_id"] == ["app-a"]


# -- the deployment's own callback under a central bridge ----------------------------------


async def _seed(kind: str, fake: FakeOAuthServer, store: FakeClientStore) -> dict:
    """Prepare the provider's client of ``kind``; returns the metadata the server publishes."""
    issuer = f"{fake.base}/a"
    if kind == "manual-secret":
        await store.put_manual("remote", issuer, "app-a", "app-secret", "client_secret_post")
    elif kind == "manual-public":
        await store.put_manual("remote", issuer, "app-a", None, "none")
    if kind == "document":
        return {"client_id_metadata_document_supported": True}
    return {}


@pytest.mark.parametrize("kind", ["document", "dynamic", "manual-secret", "manual-public"])
@pytest.mark.parametrize("operation", ["connect", "reconnect", "toggle-on"])
async def test_a_stored_client_flow_carries_the_deployments_own_callback(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore, bridge, kind: str, operation: str
):
    _, records, _, flows, _, providers = harness
    metadata = await _seed(kind, fake, client_store_fake)
    mcp = _publish(fake, registration=("/a",), **metadata)
    providers["remote"] = make_discovered_descriptor(mcp_url=mcp, scopes=["files.read"])
    if operation == "connect":
        result = await _start()
    elif operation == "reconnect":
        records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="any"))
        result = await start_reconnect(
            connection_id=CID,
            enabled_sub_services=["files"],
            return_url="/connectors",
            redirect_uri=BRIDGE_CALLBACK,
            origin=ORIGIN,
        )
    else:
        records[CID] = _record(grant=None, enabled=["notes"], granted=[])
        patched = await patch_sub_services(
            connection_id=CID,
            desired=["notes", "files"],
            return_url="/connectors",
            redirect_uri=BRIDGE_CALLBACK,
            origin=ORIGIN,
        )
        assert patched.consent_required
        assert patched.flow_id is not None
        assert patched.authorize_url is not None
        result = StartConnectResult(flow_id=patched.flow_id, authorize_url=patched.authorize_url)
    flow = flows[result.flow_id]
    assert flow.redirect_uri == CALLBACK
    assert _query(result)["redirect_uri"] == [CALLBACK]
    expected_client = {"document": DOCUMENT, "dynamic": "dyn-a"}.get(kind, "app-a")
    assert flow.client_id == expected_client
    for registration in fake.requests_to("/a/register", "POST"):
        assert registration.json["redirect_uris"] == [CALLBACK]
    assert (kind == "dynamic") == bool(fake.requests_to("/a/register"))


async def test_an_env_client_flow_on_the_same_deployment_keeps_the_bridge(harness, fake: FakeOAuthServer, bridge):
    _, _, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake), issuer=f"{fake.base}/a")
    result = await _start()
    assert flows[result.flow_id].redirect_uri == BRIDGE_CALLBACK
    assert _query(result)["redirect_uri"] == [BRIDGE_CALLBACK]


# -- the client-identity document through a whole login ------------------------------------


async def test_a_document_login_exchanges_with_no_secret_and_records_the_document_as_its_client(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore, monkeypatch
):
    _, _, _, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake, client_id_metadata_document_supported=True))
    persisted: list[ConnectionRecord] = []

    async def _capture(record, **kwargs):
        persisted.append(record.model_copy())

    import tai42_skeleton.connectors.service.connection_service.complete as complete_mod

    monkeypatch.setattr(complete_mod, "_persist", _capture)
    result = await _start()
    assert _query(result)["client_id"] == [DOCUMENT]
    fake.route("POST", "/a/token", Reply(body={"access_token": "at", "refresh_token": "rt"}))
    await complete_connect(flow_id=result.flow_id, code="c", iss=f"{fake.base}/a")
    (token_request,) = fake.requests_to("/a/token")
    assert token_request.form["client_id"] == DOCUMENT
    assert "client_secret" not in token_request.form
    assert "authorization" not in token_request.headers
    (record,) = persisted
    assert record.grant is not None
    assert record.grant.client_id == DOCUMENT
    assert client_store_fake.rows == []


# -- a refused client at the code exchange, a client gone mid-login -------------------------


async def test_a_dynamic_client_refused_at_the_code_exchange_is_marked_dead(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake, registration=("/a",)))
    result = await _start()
    fake.route("POST", "/a/token", Reply(status=401, body={"error": "invalid_client"}))
    with pytest.raises(oauth_client.CodeExchangeFailedError):
        await complete_connect(flow_id=result.flow_id, code="c", iss=f"{fake.base}/a")
    (row,) = client_store_fake.rows
    assert row.dead
    # The user's next attempt registers a new client.
    fake.route("POST", "/a/register", Reply(status=201, body={"client_id": "dyn-again", "client_secret": "s"}))
    again = await _start()
    assert _query(again)["client_id"] == ["dyn-again"]


async def test_a_manual_client_refused_at_the_code_exchange_is_never_marked_dead(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    await client_store_fake.put_manual("remote", f"{fake.base}/a", "app-a", "wrong", "client_secret_post")
    result = await _start()
    fake.route("POST", "/a/token", Reply(status=401, body={"error": "invalid_client"}))
    with pytest.raises(oauth_client.CodeExchangeFailedError):
        await complete_connect(flow_id=result.flow_id, code="c", iss=f"{fake.base}/a")
    assert not client_store_fake.rows[0].dead
    assert "mark_dead" not in client_store_fake.calls


async def test_a_client_row_removed_between_start_and_completion_fails_the_login(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, _, _, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    await client_store_fake.put_manual("remote", f"{fake.base}/a", "app-a", "s", "client_secret_post")
    result = await _start()
    await client_store_fake.delete_for_provider("remote")
    with pytest.raises(ConnectorClientRequiredError):
        await complete_connect(flow_id=result.flow_id, code="c", iss=f"{fake.base}/a")
    assert fake.requests_to("/a/token") == []


# -- disconnect ---------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["dead", "deleted"])
async def test_disconnect_with_a_dead_or_removed_client_removes_the_connection(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore, state: str
):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    if state == "dead":
        client_store_fake.add_row(
            provider_id="remote", issuer=f"{fake.base}/a", client_id="dyn-1", source="dynamic", dead=True
        )
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="dyn-1"))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert fake.requests_to("/a/revoke") == []
    assert CID in store.deleted


async def test_disconnect_revokes_with_the_grants_stored_client(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, records, store, _, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    client_store_fake.add_row(
        provider_id="remote",
        issuer=f"{fake.base}/a",
        client_id="dyn-1",
        source="dynamic",
        client_secret="dyn-secret",
        auth_method="client_secret_post",
    )
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="dyn-1"))
    fake.route("POST", "/a/revoke", Reply(status=200))
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "success"
    (revoked,) = fake.requests_to("/a/revoke")
    assert revoked.form == {"token": "old-rt", "client_id": "dyn-1", "client_secret": "dyn-secret"}
    assert CID in store.deleted


async def test_a_reconnect_flow_records_the_reconnect_operation(
    harness, fake: FakeOAuthServer, client_store_fake: FakeClientStore
):
    _, records, _, flows, _, providers = harness
    providers["remote"] = make_discovered_descriptor(mcp_url=_publish(fake))
    await client_store_fake.put_manual("remote", f"{fake.base}/a", "app-a", "s", "client_secret_post")
    records[CID] = _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="app-a"))
    result = await start_reconnect(
        connection_id=CID, enabled_sub_services=["files"], return_url="/x", redirect_uri=CALLBACK, origin=ORIGIN
    )
    assert flows[result.flow_id].operation == FlowOperation.RECONNECT
