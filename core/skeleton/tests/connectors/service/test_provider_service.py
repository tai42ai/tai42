"""The provider service behind the provider door: the suggested id, the descriptor a discovered server
becomes, the manifest mutators, which providers the door may remove, where a provider's client comes from,
and a provider's stored connections split by their session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.connectors.models import AddServerRequest
from tai42_contract.connectors.providers import SLUG_RE
from tai42_contract.plugins import PluginSpec

from tai42_skeleton.connectors.oauth.discovery import DiscoveredAuthorization
from tai42_skeleton.connectors.service import provider_service
from tai42_skeleton.connectors.service.provider_service import ProviderIdTakenError
from tai42_skeleton.connectors.store.redis_pg import StoredConnection
from tai42_skeleton.marketplace.store import InstallRecord

from ...marketplace._specs import connector_item, make_spec
from ..client_store_fake import FakeClientStore
from ..conftest import make_discovered_descriptor, make_noauth_http_descriptor, make_oauth_descriptor

MCP = "https://mcp.example.com/mcp"


def _discovered(**overrides: object) -> DiscoveredAuthorization:
    values: dict[str, object] = {
        "issuer": "https://auth.example.com",
        "authorization_endpoint": "https://auth.example.com/authorize",
        "token_endpoint": "https://auth.example.com/token",
        "registration_endpoint": "https://auth.example.com/register",
        "revocation_endpoint": None,
        "resource": MCP,
        "resource_name": None,
        "challenge_scopes": None,
        "scopes_supported": (),
        "offline_access_supported": False,
        "token_endpoint_auth_methods_supported": None,
        "client_id_document_supported": False,
        "iss_parameter_supported": False,
        "authorization_servers": ("https://auth.example.com",),
    }
    values.update(overrides)
    return DiscoveredAuthorization(**values)  # type: ignore[arg-type]


# -- the address ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com/mcp",
        "http://127.0.0.1:8080/mcp",
        "http://localhost:9000/mcp",
        "http://[::1]:9000/mcp",
    ],
)
def test_an_https_or_loopback_address_is_accepted(url: str) -> None:
    assert provider_service.check_server_url(url) == url


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://mcp.example.com/mcp", "https"),
        ("ftp://mcp.example.com/mcp", "https"),
        ("https://user:pw@mcp.example.com/mcp", "credentials"),
        ("https://mcp.example.com/mcp#part", "fragment"),
        ("https:///mcp", "host"),
        ("https://mcp.example.com/m cp", "whitespace"),
        ("mcp.example.com/mcp", "https"),
    ],
)
def test_any_other_address_is_refused_naming_why(url: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        provider_service.check_server_url(url)


# -- the suggested id ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mcp.example.com/mcp", "mcp_example_com"),
        ("https://MCP.Example.COM:8443/v1/mcp", "mcp_example_com"),
        ("https://api--tools.example.com/mcp", "api_tools_example_com"),
        ("https://42tools.example.com/mcp", "mcp_42tools_example_com"),
        ("http://127.0.0.1:9000/mcp", "mcp_127_0_0_1"),
        ("http://[::1]:9000/mcp", "mcp_1"),
    ],
)
def test_the_suggested_id_is_the_host_as_a_slug(url: str, expected: str) -> None:
    suggested = provider_service.suggest_provider_id(url, set())
    assert suggested == expected
    assert SLUG_RE.match(suggested)


def test_a_long_host_is_cut_to_64() -> None:
    host = "a" + "b" * 100 + ".example.com"
    suggested = provider_service.suggest_provider_id(f"https://{host}/mcp", set())
    assert len(suggested) == 64
    assert SLUG_RE.match(suggested)


def test_a_taken_id_gets_the_next_free_number() -> None:
    taken = {"mcp_example_com", "mcp_example_com_2"}
    assert provider_service.suggest_provider_id(MCP, taken) == "mcp_example_com_3"


def test_a_long_taken_id_keeps_its_number_within_64() -> None:
    host = "a" * 80 + ".example.com"
    first = provider_service.suggest_provider_id(f"https://{host}/mcp", set())
    second = provider_service.suggest_provider_id(f"https://{host}/mcp", {first})
    assert second.endswith("_2")
    assert len(second) == 64
    assert SLUG_RE.match(second)


# -- the descriptor ------------------------------------------------------------------------------


def test_the_descriptor_of_a_discovered_server() -> None:
    request = AddServerRequest(url=MCP, scopes=["files.read"])
    descriptor = provider_service.build_descriptor(request, _discovered(), "remote")
    assert descriptor.id == "remote"
    assert descriptor.kind == "oauth"
    assert descriptor.origin == "community"
    assert descriptor.category == "other"
    assert descriptor.oauth is None
    assert (descriptor.client_id_env, descriptor.client_secret_env, descriptor.issuer) == (None, None, None)
    assert descriptor.is_discovered
    assert descriptor.display_name == "mcp.example.com"
    assert descriptor.icon_url is None
    assert list(descriptor.sub_services) == ["default"]
    sub = descriptor.sub_services["default"]
    assert sub.display_name == "mcp.example.com"
    assert sub.scopes == ["files.read"]
    assert sub.mcp_server is not None
    assert (sub.mcp_server.type, sub.mcp_server.url) == ("http", MCP)


def test_the_descriptor_takes_the_request_names_then_the_resource_name() -> None:
    named = provider_service.build_descriptor(
        AddServerRequest(
            url=MCP,
            display_name="Team tools",
            description="Shared tools",
            icon_url="https://icons.example.com/t.png",
            category="dev-tools",
        ),
        _discovered(resource_name="Example tools"),
        "remote",
    )
    assert (named.display_name, named.description, named.icon_url, named.category) == (
        "Team tools",
        "Shared tools",
        "https://icons.example.com/t.png",
        "dev-tools",
    )
    assert named.sub_services["default"].display_name == "Team tools"
    by_resource = provider_service.build_descriptor(
        AddServerRequest(url=MCP), _discovered(resource_name="Example tools"), "remote"
    )
    assert by_resource.display_name == "Example tools"


def test_the_descriptor_refuses_a_category_that_is_not_seeded() -> None:
    with pytest.raises(ValueError, match="category 'billing-stuff' is not one of"):
        provider_service.build_descriptor(AddServerRequest(url=MCP, category="billing-stuff"), _discovered(), "x")


# -- the manifest mutators -----------------------------------------------------------------------


def test_add_appends_and_refuses_an_existing_id() -> None:
    descriptor = provider_service.build_descriptor(AddServerRequest(url=MCP), _discovered(), "remote")
    document: dict = {"mcp": []}
    provider_service.add_provider(document, descriptor)
    assert [entry["id"] for entry in document["connectors"]] == ["remote"]
    assert document["connectors"][0]["sub_services"]["default"]["mcp_server"]["url"] == MCP
    with pytest.raises(ProviderIdTakenError, match="'remote'"):
        provider_service.add_provider(document, descriptor)


def test_remove_drops_the_provider_by_id_only() -> None:
    document = {"connectors": [{"id": "keep"}, {"id": "remote"}]}
    provider_service.remove_provider(document, "remote")
    assert document["connectors"] == [{"id": "keep"}]
    with pytest.raises(KeyError):
        provider_service.remove_provider(document, "remote")


# -- removable -----------------------------------------------------------------------------------


def _install(ref: str, provider_id: str) -> InstallRecord:
    spec: PluginSpec = make_spec(
        name=ref.split("/")[1],
        namespace=ref.split("/")[0],
        provides=[connector_item(provider_id, origin="community", discovered=True)],
    )
    return InstallRecord(
        ref=ref,
        version="0.1.0",
        source="pypi",
        spec=spec.model_dump(mode="json"),
        installed_at=datetime.now(UTC),
    )


async def test_only_a_discovered_stored_client_provider_in_the_manifest_that_no_plugin_provides_is_removable(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    cs, _, _, _, _, providers = harness
    manifest = cs.ConfigService.from_app()
    stored = make_discovered_descriptor(provider_id="remote")
    pinned = make_discovered_descriptor(provider_id="pinned", issuer="https://auth.example.com")
    plugin_owned = make_discovered_descriptor(provider_id="owned")
    absent = make_discovered_descriptor(provider_id="absent")
    declared = make_oauth_descriptor()
    noauth = make_noauth_http_descriptor()
    for descriptor in (stored, pinned, plugin_owned, declared, noauth):
        providers[descriptor.id] = descriptor
    manifest.doc["connectors"] = [
        d.model_dump(mode="json", exclude_none=True) for d in (stored, pinned, plugin_owned, declared, noauth)
    ]
    providers["absent"] = absent
    monkeypatch.setattr(
        provider_service, "_live_manifest_ids", lambda: set(provider_service.manifest_provider_ids(manifest.doc))
    )

    async def installed() -> list[InstallRecord]:
        return [_install("acme/owned-tools", "owned")]

    monkeypatch.setattr(provider_service, "_installed_plugins", installed)

    assert await provider_service.provider_is_removable("remote") is True
    for provider_id in ("pinned", "owned", "acme", "httpsvc", "absent"):
        assert await provider_service.provider_is_removable(provider_id) is False, provider_id
    refusal = await provider_service.removal_refusal(plugin_owned)
    assert refusal is not None
    assert "acme/owned-tools" in refusal
    assert "uninstall" in refusal
    pinned_refusal = await provider_service.removal_refusal(pinned)
    assert pinned_refusal is not None
    assert "manifest" in pinned_refusal


# -- where a client comes from -------------------------------------------------------------------


async def test_client_sources(harness, client_store_fake: FakeClientStore) -> None:
    issuer = "https://auth.example.com"
    descriptors = [
        make_discovered_descriptor(provider_id="manualp"),
        make_discovered_descriptor(provider_id="dynp"),
        make_discovered_descriptor(provider_id="deadp"),
        make_discovered_descriptor(provider_id="nonep"),
        make_discovered_descriptor(provider_id="envp", issuer=issuer),
        make_oauth_descriptor(),
        make_noauth_http_descriptor(),
    ]
    client_store_fake.add_row(provider_id="manualp", issuer=issuer, client_id="m", source="manual")
    client_store_fake.add_row(provider_id="manualp", issuer=issuer, client_id="d", source="dynamic")
    client_store_fake.add_row(provider_id="dynp", issuer=issuer, client_id="d", source="dynamic")
    client_store_fake.add_row(provider_id="deadp", issuer=issuer, client_id="d", source="dynamic", dead=True)
    client_store_fake.add_row(
        provider_id="deadp",
        issuer=issuer,
        client_id="e",
        source="dynamic",
        secret_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    sources = await provider_service.client_sources(descriptors)
    assert sources == {
        "manualp": "manual",
        "dynp": "dynamic",
        "deadp": "unset",
        "nonep": "unset",
        "envp": "env",
        "acme": "env",
        "httpsvc": None,
    }
    assert client_store_fake.calls.count("sources") == 1


async def test_client_sources_with_the_store_off_read_unset(
    harness, client_store_fake: FakeClientStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)
    from tai42_kit.settings import reset_all_settings

    reset_all_settings()
    sources = await provider_service.client_sources(
        [make_discovered_descriptor(provider_id="remote"), make_oauth_descriptor()]
    )
    assert sources == {"remote": "unset", "acme": "env"}
    assert "sources" not in client_store_fake.calls


# -- a provider's connections --------------------------------------------------------------------


def _row(connection_id: str, provider_id: str, expires: datetime | None) -> StoredConnection:
    return StoredConnection(
        connection_id=connection_id,
        provider_id=provider_id,
        alias=connection_id,
        encrypted_blob=b"sealed",
        session_expires_at=expires,
    )


async def test_connections_split_live_and_lapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    rows = [
        _row("c1", "remote", None),
        _row("c2", "remote", now + timedelta(days=1)),
        _row("c3", "remote", now - timedelta(seconds=1)),
        _row("c4", "other", None),
    ]

    async def stored() -> list[StoredConnection]:
        return rows

    monkeypatch.setattr(provider_service, "_stored_connections", stored)
    live, lapsed = await provider_service.connections_of("remote")
    assert live == ["c1", "c2"]
    assert lapsed == ["c3"]
    assert await provider_service.connections_of("absent") == ([], [])


async def test_taken_ids_are_every_id_with_a_descriptor_a_connection_record_or_a_client_row(
    harness, client_store_fake: FakeClientStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cs, *_ = harness
    cs.ConfigService.from_app().doc["connectors"] = [{"id": "described"}]

    async def stored() -> list[StoredConnection]:
        return [_row("c1", "connected", datetime.now(UTC) - timedelta(days=1))]

    monkeypatch.setattr(provider_service, "_stored_connections", stored)
    client_store_fake.add_row(
        provider_id="clientp", issuer="https://a.example", client_id="x", source="dynamic", dead=True
    )
    assert await provider_service.taken_provider_ids() == {"described", "connected", "clientp"}
    assert await provider_service.id_taken_reason("described") == "a provider with this id is in the manifest"
    reason = await provider_service.id_taken_reason("connected")
    assert reason is not None
    assert "connection record" in reason
    reason = await provider_service.id_taken_reason("clientp")
    assert reason is not None
    assert "client credentials" in reason
    assert await provider_service.id_taken_reason("free_id") is None
