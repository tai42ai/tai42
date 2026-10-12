"""The four provider-door operations against a loopback protocol fixture, the in-memory client store and
the manifest pipeline fake: every answer of the look-up, add, remove and set-client operations, the free-id
rule, the issuer a client is stored for, and the secret kept out of every answer and every log record."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import discovery
from tai42_skeleton.connectors.service import provider_service
from tai42_skeleton.connectors.store.redis_pg import StoredConnection
from tai42_skeleton.operations import BadRequestError, ConflictError, NotFoundError, NotSupportedError
from tai42_skeleton.operations import connector_providers as ops

from ..client_store_fake import FakeClientStore
from ..conftest import CID, CID2, make_discovered_descriptor, make_oauth_descriptor
from ..oauth_fakes import FakeOAuthServer, Reply, serve

ORIGIN = "https://app.example.com"
CALLBACK = "https://app.example.com/oauth-bridge.html"
SECRET = "s3cr3t-client-value"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", ORIGIN)
    reset_all_settings()
    discovery.reset_discovery_cache()
    yield
    discovery.reset_discovery_cache()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


@pytest.fixture
def stored_rows(monkeypatch: pytest.MonkeyPatch) -> list[StoredConnection]:
    rows: list[StoredConnection] = []

    async def stored() -> list[StoredConnection]:
        return list(rows)

    monkeypatch.setattr(provider_service, "_stored_connections", stored)
    return rows


@pytest.fixture
def plugins(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    owners: dict[str, str] = {}

    async def owned() -> dict[str, str]:
        return dict(owners)

    monkeypatch.setattr(provider_service, "plugin_connector_owners", owned)
    return owners


@pytest.fixture
def door(harness, stored_rows, plugins, monkeypatch: pytest.MonkeyPatch):
    """The connection-service harness plus a manifest that carries a ``connectors`` list."""
    cs, records, _store, _flows, _events, providers = harness
    manifest = cs.ConfigService.from_app()
    manifest.doc["connectors"] = []
    # The live manifest is the persisted one in this harness (the fake pipeline does not reload).
    monkeypatch.setattr(
        provider_service, "_live_manifest_ids", lambda: set(provider_service.manifest_provider_ids(manifest.doc))
    )
    return cs, manifest, providers, records


def _publish(
    fake: FakeOAuthServer,
    servers: tuple[str, ...] = ("/a",),
    *,
    registration: tuple[str, ...] = (),
    resource_name: str | None = None,
    scopes: list[str] | None = None,
    **metadata,
) -> str:
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    document: dict = {"resource": mcp, "authorization_servers": [f"{fake.base}{p}" for p in servers]}
    if resource_name is not None:
        document["resource_name"] = resource_name
    if scopes is not None:
        document["scopes_supported"] = scopes
    fake.json("/prm", document)
    for path in servers:
        extra = dict(metadata)
        if path in registration:
            extra["registration_endpoint"] = f"{fake.base}{path}/register"
        fake.authorization_server(path, **extra)
    return mcp


def _register(providers: dict, manifest, descriptor) -> None:
    providers[descriptor.id] = descriptor
    manifest.doc["connectors"].append(descriptor.model_dump(mode="json", exclude_none=True))


def _connection(connection_id: str, provider_id: str, *, lapsed: bool) -> StoredConnection:
    now = datetime.now(UTC)
    return StoredConnection(
        connection_id=connection_id,
        provider_id=provider_id,
        alias=connection_id[:8],
        encrypted_blob=b"sealed",
        session_expires_at=now - timedelta(hours=1) if lapsed else now + timedelta(days=1),
    )


def _oauth_record(connection_id: str, provider_id: str, issuer: str) -> ConnectionRecord:
    return ConnectionRecord(
        connection_id=connection_id,
        provider_id=provider_id,
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=["default"],
        granted_scopes=[],
        access_token=SecretStr("old-at"),
        refresh_token=SecretStr("old-rt"),
        auth_health_state=AuthHealthState.HEALTHY,
        grant=ConnectionGrant(issuer=issuer, client_id="gone"),
        created_at=datetime.now(UTC),
    )


# -- every operation refuses with the store off --------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: ops.discover_connector_server(url="https://mcp.example.com/mcp", origin=ORIGIN),
        lambda: ops.add_connector_provider(url="https://mcp.example.com/mcp"),
        lambda: ops.remove_connector_provider(provider_id="remote"),
        lambda: ops.set_connector_client(provider_id="remote", client_id="app"),
    ],
)
async def test_every_operation_refuses_with_the_store_off(monkeypatch: pytest.MonkeyPatch, call) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    reset_all_settings()
    with pytest.raises(NotSupportedError) as info:
        await call()
    assert info.value.extra["code"] == "connectors-not-configured"


# -- the address is checked before any request ---------------------------------------------------


@pytest.mark.parametrize("url", ["http://mcp.example.com/mcp", "https://u:p@mcp.example.com/mcp", "https://x.test/m#f"])
async def test_an_address_that_is_not_https_is_refused_before_any_request(door, fake: FakeOAuthServer, url) -> None:
    with pytest.raises(BadRequestError):
        await ops.discover_connector_server(url=url, origin=ORIGIN)
    with pytest.raises(BadRequestError):
        await ops.add_connector_provider(url=url)
    assert fake.received == []


async def test_a_look_up_names_exactly_one_of_an_address_and_a_provider(door) -> None:
    with pytest.raises(BadRequestError, match="exactly one"):
        await ops.discover_connector_server(origin=ORIGIN)
    with pytest.raises(BadRequestError, match="exactly one"):
        await ops.discover_connector_server(url="https://mcp.example.com/mcp", provider_id="remote", origin=ORIGIN)


# -- look up -------------------------------------------------------------------------------------


async def test_a_look_up_by_address_returns_the_facts_and_persists_nothing(
    door, fake: FakeOAuthServer, client_store_fake: FakeClientStore
) -> None:
    _, manifest, _, _ = door
    mcp = _publish(fake, servers=("/b", "/a"), registration=("/b",), resource_name="Team tools", scopes=["files"])
    view = await ops.discover_connector_server(url=mcp, origin=ORIGIN)
    assert view["url"] == mcp
    assert view["resource"] == mcp
    assert view["resource_name"] == "Team tools"
    assert view["issuer"] == f"{fake.base}/b"
    assert view["authorization_servers"] == [f"{fake.base}/b", f"{fake.base}/a"]
    assert view["client_issuer"] is None
    assert view["registration_supported"] is True
    assert view["automatic_client"] is True
    assert view["scopes_supported"] == ["files"]
    assert view["suggested_id"] == "mcp_127_0_0_1"
    assert view["suggested_display_name"] == "Team tools"
    assert view["redirect_uri"] == CALLBACK
    assert manifest.doc["connectors"] == []
    assert client_store_fake.rows == []


async def test_a_look_up_suggests_an_id_past_every_taken_one(
    door, fake: FakeOAuthServer, client_store_fake: FakeClientStore, stored_rows
) -> None:
    _, manifest, _, _ = door
    manifest.doc["connectors"].append({"id": "mcp_127_0_0_1"})
    stored_rows.append(_connection(CID, "mcp_127_0_0_1_2", lapsed=True))
    client_store_fake.add_row(provider_id="mcp_127_0_0_1_3", issuer="https://x.example", client_id="c", source="manual")
    view = await ops.discover_connector_server(url=_publish(fake), origin=ORIGIN)
    assert view["suggested_id"] == "mcp_127_0_0_1_4"
    assert view["suggested_display_name"] == "127.0.0.1"


async def test_a_server_without_registration_needs_client_credentials(door, fake: FakeOAuthServer) -> None:
    view = await ops.discover_connector_server(url=_publish(fake), origin=ORIGIN)
    assert view["registration_supported"] is False
    assert view["automatic_client"] is False


async def test_the_client_identity_document_counts_only_from_a_document_origin(door, fake: FakeOAuthServer) -> None:
    mcp = _publish(fake, client_id_metadata_document_supported=True)
    assert (await ops.discover_connector_server(url=mcp, origin=ORIGIN))["automatic_client"] is True
    plain = await ops.discover_connector_server(url=mcp, origin="http://localhost:5173")
    assert plain["automatic_client"] is False


async def test_an_origin_off_the_allow_list_gives_no_redirect_uri(door, fake: FakeOAuthServer) -> None:
    view = await ops.discover_connector_server(url=_publish(fake), origin="https://elsewhere.example.com")
    assert view["redirect_uri"] is None
    assert (await ops.discover_connector_server(url=_publish(fake)))["redirect_uri"] is None


@pytest.mark.parametrize(
    ("setup", "step_text"),
    [
        ("no-metadata", "publishes no OAuth metadata"),
        ("mismatch", "names a different resource"),
        ("no-pkce", "PKCE"),
    ],
)
async def test_a_failed_discovery_is_a_400_naming_the_step(door, fake: FakeOAuthServer, setup, step_text) -> None:
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    if setup == "mismatch":
        fake.json("/prm", {"resource": "https://other.example.com/mcp", "authorization_servers": [f"{fake.base}/a"]})
    elif setup == "no-pkce":
        fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}/a"]})
        fake.authorization_server("/a", pkce=[])
    with pytest.raises(BadRequestError, match=step_text) as info:
        await ops.discover_connector_server(url=mcp, origin=ORIGIN)
    assert info.value.extra["step"] in ("resource-metadata", "resource-mismatch", "pkce-unsupported")
    if setup == "no-metadata":
        assert "plain MCP server" in info.value.message


async def test_a_private_address_refused_by_the_guard_names_the_allowance(
    door, fake: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAI_URL_GUARD_ALLOW_CIDRS")
    reset_all_settings()
    with pytest.raises(BadRequestError, match="TAI_URL_GUARD_ALLOW_CIDRS") as info:
        await ops.discover_connector_server(url=_publish(fake), origin=ORIGIN)
    assert info.value.extra["step"] == "url-refused"
    assert fake.received == []


async def test_a_look_up_by_provider(door, fake: FakeOAuthServer, client_store_fake: FakeClientStore) -> None:
    _, manifest, providers, _ = door
    mcp = _publish(fake, servers=("/b", "/a"), registration=("/b",))
    _register(providers, manifest, make_discovered_descriptor(mcp_url=mcp))
    issuer_a = f"{fake.base}/a"
    await client_store_fake.put_manual("remote", issuer_a, "app-a", "secret", "client_secret_post")
    view = await ops.discover_connector_server(provider_id="remote", origin=ORIGIN)
    assert view["issuer"] == issuer_a
    assert view["client_issuer"] == issuer_a
    assert view["authorization_servers"] == [f"{fake.base}/b", issuer_a]
    assert view["registration_supported"] is False
    assert view["automatic_client"] is True
    assert (view["suggested_id"], view["suggested_display_name"]) == ("remote", "Remote")
    by_address = await ops.discover_connector_server(url=mcp, origin=ORIGIN)
    assert (by_address["issuer"], by_address["client_issuer"]) == (f"{fake.base}/b", None)


async def test_a_look_up_by_provider_names_a_client_issuer_the_server_no_longer_lists(
    door, fake: FakeOAuthServer, client_store_fake: FakeClientStore
) -> None:
    _, manifest, providers, _ = door
    _register(providers, manifest, make_discovered_descriptor(mcp_url=_publish(fake, servers=("/b",))))
    await client_store_fake.put_manual("remote", f"{fake.base}/a", "app-a", "secret", "client_secret_post")
    view = await ops.discover_connector_server(provider_id="remote", origin=ORIGIN)
    assert view["issuer"] == f"{fake.base}/b"
    assert view["client_issuer"] == f"{fake.base}/a"
    assert view["automatic_client"] is False


async def test_a_look_up_of_an_env_client_provider_uses_its_pin(door, fake: FakeOAuthServer) -> None:
    _, manifest, providers, _ = door
    mcp = _publish(fake, servers=("/b", "/a"))
    _register(providers, manifest, make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"))
    view = await ops.discover_connector_server(provider_id="remote", origin=ORIGIN)
    assert view["issuer"] == f"{fake.base}/a"
    assert view["automatic_client"] is True


async def test_a_look_up_by_provider_refuses_an_unknown_or_a_declared_one(door) -> None:
    _, _, providers, _ = door
    with pytest.raises(NotFoundError):
        await ops.discover_connector_server(provider_id="nobody", origin=ORIGIN)
    with pytest.raises(BadRequestError, match="not a discovered provider"):
        await ops.discover_connector_server(provider_id="acme", origin=ORIGIN)
    assert "acme" in providers


# -- add -----------------------------------------------------------------------------------------


async def test_add_writes_the_discovered_descriptor_and_answers_its_catalog_entry(door, fake: FakeOAuthServer) -> None:
    _, manifest, _, _ = door
    mcp = _publish(fake, registration=("/a",), resource_name="Team tools")
    result = await ops.add_connector_provider(url=mcp, scopes=["files.read"])
    (written,) = manifest.doc["connectors"]
    assert written["id"] == "mcp_127_0_0_1"
    assert written["origin"] == "community"
    assert written["category"] == "other"
    server = written["sub_services"]["default"]["mcp_server"]
    assert (server["type"], server["url"]) == ("http", mcp)
    assert written["sub_services"]["default"]["scopes"] == ["files.read"]
    assert "oauth" not in written
    assert "client_id_env" not in written
    provider = result["provider"]
    assert provider["id"] == "mcp_127_0_0_1"
    assert provider["display_name"] == "Team tools"
    assert provider["removable"] is True
    assert provider["client_source"] == "unset"
    assert result["fanout"] == {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"}


async def test_add_under_an_id_in_the_manifest_is_a_409(door, fake: FakeOAuthServer) -> None:
    _, manifest, _, _ = door
    manifest.doc["connectors"].append({"id": "remote"})
    with pytest.raises(ConflictError, match="manifest"):
        await ops.add_connector_provider(url=_publish(fake), provider_id="remote")
    assert manifest.doc["connectors"] == [{"id": "remote"}]


@pytest.mark.parametrize("lapsed", [False, True])
async def test_add_under_an_id_with_a_connection_record_is_a_409_and_deletes_nothing(
    door, fake: FakeOAuthServer, stored_rows, lapsed: bool
) -> None:
    _, manifest, _, _ = door
    stored_rows.append(_connection(CID, "remote", lapsed=lapsed))
    with pytest.raises(ConflictError, match="connection record"):
        await ops.add_connector_provider(url=_publish(fake), provider_id="remote")
    assert manifest.doc["connectors"] == []
    assert len(stored_rows) == 1


async def test_add_under_an_id_with_a_client_row_is_a_409_and_deletes_nothing(
    door, fake: FakeOAuthServer, client_store_fake: FakeClientStore
) -> None:
    _, manifest, _, _ = door
    client_store_fake.add_row(
        provider_id="remote", issuer="https://x.example", client_id="c", source="dynamic", dead=True
    )
    with pytest.raises(ConflictError, match="client credentials"):
        await ops.add_connector_provider(url=_publish(fake), provider_id="remote")
    assert manifest.doc["connectors"] == []
    assert len(client_store_fake.rows) == 1


async def test_add_without_an_id_skips_every_taken_one(door, fake: FakeOAuthServer, stored_rows) -> None:
    stored_rows.append(_connection(CID, "mcp_127_0_0_1", lapsed=True))
    result = await ops.add_connector_provider(url=_publish(fake))
    assert result["provider"]["id"] == "mcp_127_0_0_1_2"


async def test_add_refuses_a_bad_id_category_or_icon(door, fake: FakeOAuthServer) -> None:
    mcp = _publish(fake)
    with pytest.raises(BadRequestError):
        await ops.add_connector_provider(url=mcp, provider_id="Bad-Id")
    with pytest.raises(BadRequestError, match="category"):
        await ops.add_connector_provider(url=mcp, category="nowhere")
    with pytest.raises(BadRequestError):
        await ops.add_connector_provider(url=mcp, icon_url="http://icons.example.com/i.png")


async def test_add_of_a_server_that_fails_discovery_writes_nothing(door, fake: FakeOAuthServer) -> None:
    _, manifest, _, _ = door
    fake.protected_mcp("/mcp", pointer=None)
    with pytest.raises(BadRequestError, match="publishes no OAuth metadata"):
        await ops.add_connector_provider(url=f"{fake.base}/mcp")
    assert manifest.doc["connectors"] == []


async def test_add_losing_a_race_for_its_id_is_a_409(door, fake: FakeOAuthServer, monkeypatch) -> None:
    _, manifest, _, _ = door
    real_apply = manifest.apply_change

    async def racing(mutator):
        manifest.doc["connectors"].append({"id": "remote"})
        return await real_apply(mutator)

    monkeypatch.setattr(manifest, "apply_change", racing)
    with pytest.raises(ConflictError, match="'remote'"):
        await ops.add_connector_provider(url=_publish(fake), provider_id="remote")


# -- remove --------------------------------------------------------------------------------------


async def test_remove_an_unknown_provider_is_a_404(door) -> None:
    with pytest.raises(NotFoundError):
        await ops.remove_connector_provider(provider_id="nobody")


async def test_remove_a_provider_that_is_not_removable_is_a_409_saying_where_it_is_managed(
    door, plugins: dict[str, str]
) -> None:
    _, manifest, providers, _ = door
    _register(providers, manifest, make_oauth_descriptor())
    with pytest.raises(ConflictError, match="manifest"):
        await ops.remove_connector_provider(provider_id="acme")
    _register(providers, manifest, make_discovered_descriptor(provider_id="owned"))
    plugins["owned"] = "acme/owned-tools"
    with pytest.raises(ConflictError, match="acme/owned-tools"):
        await ops.remove_connector_provider(provider_id="owned")
    assert {entry["id"] for entry in manifest.doc["connectors"]} == {"acme", "owned"}


async def test_remove_is_refused_while_a_live_connection_exists(door, stored_rows) -> None:
    _, manifest, providers, _ = door
    _register(providers, manifest, make_discovered_descriptor())
    stored_rows.extend([_connection(CID, "remote", lapsed=False), _connection(CID2, "remote", lapsed=True)])
    with pytest.raises(ConflictError, match="1 live connection"):
        await ops.remove_connector_provider(provider_id="remote")
    assert [entry["id"] for entry in manifest.doc["connectors"]] == ["remote"]


async def test_remove_purges_a_lapsed_connection_then_the_provider_then_its_clients(
    door, fake: FakeOAuthServer, stored_rows, client_store_fake: FakeClientStore, monkeypatch
) -> None:
    cs, manifest, providers, records = door
    _register(providers, manifest, make_discovered_descriptor(mcp_url=_publish(fake)))
    stored_rows.append(_connection(CID, "remote", lapsed=True))
    records[CID] = _oauth_record(CID, "remote", f"{fake.base}/a")
    client_store_fake.add_row(provider_id="remote", issuer="https://x.example", client_id="a", source="manual")
    client_store_fake.add_row(provider_id="remote", issuer="https://x.example", client_id="b", source="dynamic")
    order: list[str] = []
    real_disconnect = cs.disconnect

    async def recorded_disconnect(*, connection_id):
        order.append(f"disconnect:{connection_id}")
        return await real_disconnect(connection_id=connection_id)

    monkeypatch.setattr(ops.connection_service, "disconnect", recorded_disconnect)
    result = await ops.remove_connector_provider(provider_id="remote")
    assert order == [f"disconnect:{CID}"]
    assert CID not in records
    assert manifest.doc["connectors"] == []
    assert result == {"provider_id": "remote", "removed_clients": 2, "fanout": result["fanout"]}
    assert result["fanout"] is not None
    assert client_store_fake.rows == []
    assert client_store_fake.calls[-1] == "delete_for_provider"


async def test_a_failed_manifest_change_leaves_the_clients(
    door, client_store_fake: FakeClientStore, monkeypatch
) -> None:
    _, manifest, providers, _ = door
    _register(providers, manifest, make_discovered_descriptor())
    client_store_fake.add_row(provider_id="remote", issuer="https://x.example", client_id="a", source="manual")

    async def failing(mutator):
        raise RuntimeError("the manifest write failed")

    monkeypatch.setattr(manifest, "apply_change", failing)
    with pytest.raises(RuntimeError, match="the manifest write failed"):
        await ops.remove_connector_provider(provider_id="remote")
    assert len(client_store_fake.rows) == 1


async def test_a_failed_client_deletion_after_the_removal_is_raised(
    door, client_store_fake: FakeClientStore, monkeypatch
) -> None:
    _, manifest, providers, _ = door
    _register(providers, manifest, make_discovered_descriptor())

    async def failing(provider_id: str) -> int:
        raise ConnectionError("the client store is unreachable")

    monkeypatch.setattr(client_store_fake, "delete_for_provider", failing)
    with pytest.raises(ConnectionError, match="unreachable"):
        await ops.remove_connector_provider(provider_id="remote")
    assert manifest.doc["connectors"] == []


async def test_a_rerun_of_a_remove_whose_client_deletion_failed_deletes_the_rows_and_frees_the_id(
    door, client_store_fake: FakeClientStore, monkeypatch
) -> None:
    _, manifest, providers, _ = door
    _register(providers, manifest, make_discovered_descriptor())
    client_store_fake.add_row(provider_id="remote", issuer="https://a.example", client_id="m", source="manual")
    real = client_store_fake.delete_for_provider

    async def failing(provider_id: str) -> int:
        raise ConnectionError("the client store is unreachable")

    monkeypatch.setattr(client_store_fake, "delete_for_provider", failing)
    with pytest.raises(ConnectionError, match="unreachable"):
        await ops.remove_connector_provider(provider_id="remote")
    # The manifest change went through; its reload drops the provider from the registry.
    providers.pop("remote")
    monkeypatch.setattr(client_store_fake, "delete_for_provider", real)

    rerun = await ops.remove_connector_provider(provider_id="remote")

    assert rerun == {"provider_id": "remote", "removed_clients": 1, "fanout": None}
    assert not await client_store_fake.has_rows("remote")
    assert await provider_service.id_taken_reason("remote") is None


async def test_removing_an_unknown_id_with_no_client_rows_is_not_found(
    door, client_store_fake: FakeClientStore
) -> None:
    with pytest.raises(NotFoundError, match="provider 'remote' not found"):
        await ops.remove_connector_provider(provider_id="remote")


@pytest.mark.parametrize("holder", ["manifest", "plugin", "connection"])
async def test_client_rows_under_an_id_still_held_are_not_deleted_by_a_remove_of_an_unregistered_id(
    door, client_store_fake: FakeClientStore, plugins, stored_rows, holder: str
) -> None:
    """Rows are deleted by the re-run only when nothing holds the id any more: not the persisted manifest, not
    an installed plugin, not a connection record."""
    _, manifest, _providers, _ = door
    client_store_fake.add_row(provider_id="remote", issuer="https://a.example", client_id="m", source="manual")
    if holder == "manifest":
        manifest.doc["connectors"].append(make_discovered_descriptor().model_dump(mode="json", exclude_none=True))
    elif holder == "plugin":
        plugins["remote"] = "acme-plugin@1.0.0"
    else:
        stored_rows.append(_connection(CID, "remote", lapsed=True))

    with pytest.raises(NotFoundError, match="provider 'remote' not found"):
        await ops.remove_connector_provider(provider_id="remote")
    assert await client_store_fake.has_rows("remote")


# -- set client ----------------------------------------------------------------------------------


@pytest.fixture
def stored_provider(door, fake: FakeOAuthServer):
    """A stored-client provider whose server lists the given sign-in services; returns their issuers."""
    _, manifest, providers, _ = door

    def make(*servers: str, **metadata) -> list[str]:
        _register(providers, manifest, make_discovered_descriptor(mcp_url=_publish(fake, servers=servers, **metadata)))
        return [f"{fake.base}{path}" for path in servers]

    return make


async def test_set_client_with_no_issuer_and_no_stored_one_is_refused_naming_the_listed_ones(
    stored_provider, client_store_fake: FakeClientStore
) -> None:
    (issuer,) = stored_provider("/a")
    with pytest.raises(BadRequestError) as info:
        await ops.set_connector_client(provider_id="remote", client_id="app", client_secret=SECRET)
    assert info.value.extra == {"code": "connector-client-issuer-required", "issuers": [issuer]}
    assert client_store_fake.rows == []


async def test_set_client_stores_the_row_for_the_chosen_issuer(
    stored_provider, client_store_fake: FakeClientStore
) -> None:
    issuer_a, issuer_b = stored_provider("/a", "/b")
    result = await ops.set_connector_client(
        provider_id="remote", client_id="app", client_secret=SECRET, issuer=issuer_b
    )
    (row,) = client_store_fake.rows
    assert (row.issuer, row.client_id, row.source) == (issuer_b, "app", "manual")
    assert result["provider"]["client_source"] == "manual"
    assert result["fanout"] is None
    stored = await client_store_fake.manual_issuer("remote")
    assert stored == issuer_b != issuer_a


async def test_set_client_refuses_an_unlisted_issuer(stored_provider, client_store_fake: FakeClientStore) -> None:
    issuer_a, issuer_b = stored_provider("/a", "/b")
    with pytest.raises(BadRequestError, match="does not list") as info:
        await ops.set_connector_client(
            provider_id="remote", client_id="app", client_secret=SECRET, issuer="https://elsewhere.example"
        )
    assert issuer_a in info.value.message
    assert issuer_b in info.value.message
    assert client_store_fake.rows == []


async def test_a_changed_secret_keeps_the_sign_in_service_of_the_app(
    stored_provider, client_store_fake: FakeClientStore
) -> None:
    _issuer_b, issuer_a = stored_provider("/b", "/a")
    await client_store_fake.put_manual("remote", issuer_a, "app", "old-secret", "client_secret_post")
    await ops.set_connector_client(provider_id="remote", client_id="app", client_secret=SECRET)
    (row,) = client_store_fake.rows
    assert row.issuer == issuer_a


async def test_set_client_with_a_stored_issuer_no_longer_listed_needs_an_issuer(
    stored_provider, client_store_fake: FakeClientStore
) -> None:
    (issuer_b,) = stored_provider("/b")
    await client_store_fake.put_manual("remote", "https://gone.example", "app", "old", "client_secret_post")
    with pytest.raises(BadRequestError) as info:
        await ops.set_connector_client(provider_id="remote", client_id="app", client_secret=SECRET)
    assert info.value.extra["code"] == "connector-client-issuer-required"
    assert info.value.extra["issuers"] == [issuer_b]


@pytest.mark.parametrize(
    ("advertised", "secret", "requested", "expected"),
    [
        (None, SECRET, None, "client_secret_post"),
        (["client_secret_basic", "client_secret_post"], SECRET, None, "client_secret_post"),
        (["client_secret_basic"], SECRET, None, "client_secret_basic"),
        (["none", "client_secret_basic"], None, None, "none"),
        (["client_secret_post", "client_secret_basic"], SECRET, "client_secret_basic", "client_secret_basic"),
    ],
)
async def test_the_authentication_method(
    stored_provider, client_store_fake: FakeClientStore, advertised, secret, requested, expected
) -> None:
    metadata = {} if advertised is None else {"token_endpoint_auth_methods_supported": advertised}
    (issuer,) = stored_provider("/a", **metadata)
    await ops.set_connector_client(
        provider_id="remote",
        client_id="app",
        client_secret=secret,
        token_endpoint_auth_method=requested,
        issuer=issuer,
    )
    stored = await client_store_fake.get("remote", issuer, "app")
    assert stored is not None
    assert stored.auth_method == expected


@pytest.mark.parametrize(
    ("advertised", "secret", "requested"),
    [
        (["private_key_jwt"], SECRET, None),
        (["client_secret_post"], None, None),
        (["client_secret_post"], SECRET, "client_secret_basic"),
        (None, SECRET, "none"),
        (None, None, "client_secret_post"),
    ],
)
async def test_a_method_the_server_does_not_list_or_that_does_not_fit_the_secret_is_refused(
    stored_provider, client_store_fake: FakeClientStore, advertised, secret, requested
) -> None:
    metadata = {} if advertised is None else {"token_endpoint_auth_methods_supported": advertised}
    (issuer,) = stored_provider("/a", **metadata)
    with pytest.raises(BadRequestError):
        await ops.set_connector_client(
            provider_id="remote",
            client_id="app",
            client_secret=secret,
            token_endpoint_auth_method=requested,
            issuer=issuer,
        )
    assert client_store_fake.rows == []


async def test_set_client_refuses_an_unknown_a_declared_and_an_env_client_provider(door, fake: FakeOAuthServer) -> None:
    _, manifest, providers, _ = door
    with pytest.raises(NotFoundError):
        await ops.set_connector_client(provider_id="nobody", client_id="app")
    with pytest.raises(BadRequestError, match="no env client"):
        await ops.set_connector_client(provider_id="acme", client_id="app")
    _register(providers, manifest, make_discovered_descriptor(provider_id="pinned", issuer="https://a.example"))
    with pytest.raises(BadRequestError, match="no env client"):
        await ops.set_connector_client(provider_id="pinned", client_id="app")
    assert fake.received == []


async def test_logins_then_go_to_the_chosen_issuer(
    door, stored_provider, fake: FakeOAuthServer, client_store_fake: FakeClientStore
) -> None:
    cs, *_ = door
    _issuer_a, issuer_b = stored_provider("/a", "/b")
    await ops.set_connector_client(provider_id="remote", client_id="app", client_secret=SECRET, issuer=issuer_b)
    result = await cs.start_connect(
        provider_id="remote",
        alias="team",
        enabled_sub_services=["files"],
        config_values={},
        return_url="/connectors",
        redirect_uri=CALLBACK,
        origin=ORIGIN,
    )
    assert result.authorize_url.startswith(f"{issuer_b}/authorize?")


# -- the secret ----------------------------------------------------------------------------------


async def test_the_secret_is_in_no_answer_no_log_record_and_not_in_the_manifest(
    door, stored_provider, client_store_fake: FakeClientStore, caplog: pytest.LogCaptureFixture
) -> None:
    _, manifest, _, _ = door
    caplog.set_level(logging.DEBUG)
    (issuer,) = stored_provider("/a", token_endpoint_auth_methods_supported=["client_secret_basic"])
    answers: list[object] = []
    attempts: list[dict[str, Any]] = [
        {},
        {"issuer": "https://elsewhere.example"},
        {"issuer": issuer, "token_endpoint_auth_method": "none"},
        {"issuer": issuer, "token_endpoint_auth_method": "client_secret_post"},
        {"issuer": issuer},
    ]
    for kwargs in attempts:
        try:
            answers.append(
                await ops.set_connector_client(provider_id="remote", client_id="app", client_secret=SECRET, **kwargs)
            )
        except (BadRequestError, NotFoundError) as exc:
            answers.append((exc.message, exc.extra))
    assert len(answers) == 5
    assert SECRET not in repr(answers)
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    assert all(SECRET not in repr(record.args) for record in caplog.records)
    assert SECRET not in repr(manifest.doc)
    stored = await client_store_fake.get("remote", issuer, "app")
    assert stored is not None
    assert stored.client_secret == SECRET


async def test_a_discovery_failure_during_set_client_is_a_400(door, fake: FakeOAuthServer) -> None:
    _, manifest, providers, _ = door
    fake.protected_mcp("/mcp", pointer=None)
    fake.route("GET", "/.well-known/oauth-protected-resource/mcp", Reply(status=404))
    _register(providers, manifest, make_discovered_descriptor(mcp_url=f"{fake.base}/mcp"))
    with pytest.raises(BadRequestError, match="publishes no OAuth metadata"):
        await ops.set_connector_client(provider_id="remote", client_id="app")


# -- the catalog ---------------------------------------------------------------------------------


async def test_the_catalog_says_which_providers_are_removable_and_where_their_clients_come_from(
    door, plugins: dict[str, str], client_store_fake: FakeClientStore, monkeypatch
) -> None:
    from tai42_skeleton.operations import connectors as conn_ops

    _, manifest, providers, _ = door
    added = make_discovered_descriptor(provider_id="added")
    owned = make_discovered_descriptor(provider_id="owned")
    pinned = make_discovered_descriptor(provider_id="pinned", issuer="https://a.example")
    for descriptor in (added, owned, pinned, make_oauth_descriptor()):
        _register(providers, manifest, descriptor)
    plugins["owned"] = "acme/owned-tools"
    client_store_fake.add_row(provider_id="added", issuer="https://a.example", client_id="m", source="manual")

    async def no_categories() -> list:
        return []

    monkeypatch.setattr(
        conn_ops, "list_providers", lambda: [providers[p] for p in ("added", "owned", "pinned", "acme")]
    )
    monkeypatch.setattr(conn_ops, "fetch_categories", no_categories)
    catalog = await conn_ops.list_connector_providers()
    seen = {entry["id"]: (entry["removable"], entry["client_source"]) for entry in catalog["providers"]}
    assert seen == {
        "added": (True, "manual"),
        "owned": (False, "unset"),
        "pinned": (False, "env"),
        "acme": (False, "env"),
    }
    assert client_store_fake.calls.count("sources") == 1
