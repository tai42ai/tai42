"""The resolved-authorization seam.

Endpoints, the issuer pin and the caller's binding, clients, scopes, the redirect check, grants.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.errors import ConnectorClientRequiredError, OperatorMisconfiguredError
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_contract.errors import ErrorKind, error_kind
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import authorization, discovery
from tai42_skeleton.connectors.oauth.authorization import (
    GrantUnservableError,
    OAuthAuthorizationCancelledError,
    OAuthIssuerMismatchError,
    ResolvedAuthorization,
    check_redirect_issuer,
    requested_scopes,
    resolve_authorization,
    resolve_client,
    resolve_for_grant,
)
from tai42_skeleton.connectors.oauth.discovery import OAuthDiscoveryError

from .conftest import CID, make_discovered_descriptor, make_noauth_stdio_descriptor, make_oauth_descriptor
from .oauth_fakes import FakeOAuthServer, serve


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    reset_all_settings()


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    discovery.reset_discovery_cache()
    yield
    discovery.reset_discovery_cache()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


def _two_servers(fake: FakeOAuthServer, order: tuple[str, ...] = ("/b", "/a")) -> str:
    """An MCP server at ``/mcp`` listing the authorization servers at ``order``; returns the MCP URL."""
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}{p}" for p in order]})
    for path in order:
        fake.authorization_server(path, revocation_endpoint=f"{fake.base}{path}/revoke")
    return mcp


def _resolved(**overrides) -> ResolvedAuthorization:
    base = {
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": "https://as.example.com/token",
        "revocation_endpoint": None,
        "registration_endpoint": None,
        "issuer": "https://as.example.com",
        "resource": "https://mcp.example.com/mcp",
        "challenge_scopes": None,
        "scopes_supported": (),
        "offline_access_supported": False,
        "token_endpoint_auth_methods_supported": None,
        "client_id_document_supported": False,
        "iss_parameter_supported": False,
        "guarded": True,
    }
    base.update(overrides)
    return ResolvedAuthorization(**base)


def _record(*, granted: list[str], wanted: list[str]) -> ConnectionRecord:
    now = datetime.now(UTC)
    return ConnectionRecord(
        connection_id=CID,
        provider_id="remote",
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=["files"],
        granted_scopes=granted,
        scopes_wanted=wanted,
        access_token=SecretStr("at"),
        auth_health_state=AuthHealthState.HEALTHY,
        created_at=now,
    )


# -- endpoints ------------------------------------------------------------------------


async def test_a_declared_provider_resolves_from_its_descriptor_without_discovery():
    resolved = await resolve_authorization(make_oauth_descriptor())
    assert resolved.authorization_endpoint == "https://acme.test/authorize"
    assert resolved.token_endpoint == "https://acme.test/token"
    assert resolved.revocation_endpoint == "https://acme.test/revoke"
    assert resolved.issuer is None
    assert resolved.resource is None
    assert not resolved.guarded
    assert not resolved.iss_parameter_supported


async def test_a_no_auth_descriptor_is_a_caller_bug():
    with pytest.raises(RuntimeError, match="no-auth"):
        await resolve_authorization(make_noauth_stdio_descriptor())


async def test_a_discovered_provider_resolves_through_discovery(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    resolved = await resolve_authorization(make_discovered_descriptor(mcp_url=mcp))
    assert resolved.guarded
    assert resolved.issuer == f"{fake.base}/b"
    assert resolved.token_endpoint == f"{fake.base}/b/token"
    assert resolved.resource == mcp


# -- the issuer pin and the caller's binding -----------------------------------------


async def test_the_pin_chooses_its_server_even_listed_second(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    resolved = await resolve_authorization(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"))
    assert resolved.issuer == f"{fake.base}/a"
    assert resolved.token_endpoint == f"{fake.base}/a/token"


async def test_a_pin_that_differs_by_a_trailing_slash_does_not_match(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    with pytest.raises(OAuthDiscoveryError) as info:
        await resolve_authorization(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a/"))
    assert info.value.step == "issuer-mismatch"


async def test_a_pin_the_server_does_not_list(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    with pytest.raises(OAuthDiscoveryError) as info:
        await resolve_authorization(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/c"))
    assert info.value.step == "issuer-mismatch"


async def test_a_callers_bound_issuer_is_honoured_without_a_pin(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    resolved = await resolve_authorization(make_discovered_descriptor(mcp_url=mcp), bound_issuer=f"{fake.base}/a")
    assert resolved.issuer == f"{fake.base}/a"


async def test_the_pin_wins_over_the_callers_issuer(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    descriptor = make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/b")
    resolved = await resolve_authorization(descriptor, bound_issuer=f"{fake.base}/a")
    assert resolved.issuer == f"{fake.base}/b"


# -- clients --------------------------------------------------------------------------


async def test_the_env_client_of_a_declared_provider(oauth_client_env):
    descriptor = make_oauth_descriptor()
    client = await resolve_client(descriptor, authorization._declared(descriptor))
    assert client.client_id == "client-123"
    assert client.client_secret == "secret-xyz"
    assert client.auth_method == "client_secret_post"
    assert client.source == "env"
    assert "secret-xyz" not in repr(client)


async def test_an_unset_env_variable_is_an_operator_misconfiguration():
    descriptor = make_oauth_descriptor()
    with pytest.raises(OperatorMisconfiguredError) as info:
        await resolve_client(descriptor, authorization._declared(descriptor))
    assert info.value.env_var == "ACME_CLIENT_ID"
    assert info.value.provider_id == "acme"


async def test_a_stored_client_provider_needs_a_client_this_deployment_does_not_have():
    with pytest.raises(ConnectorClientRequiredError) as info:
        await resolve_client(make_discovered_descriptor(), _resolved())
    assert info.value.provider_id == "remote"
    assert error_kind(info.value) is ErrorKind.UNAVAILABLE


async def test_a_grant_issued_to_a_stored_client_is_not_served_by_the_env_client(oauth_client_env):
    descriptor = make_oauth_descriptor()
    with pytest.raises(ConnectorClientRequiredError):
        await resolve_client(descriptor, authorization._declared(descriptor), client_id="stored-1")


# -- scopes ---------------------------------------------------------------------------


def test_descriptor_scopes_are_sent_unchanged_even_when_the_server_lists_offline_access():
    descriptor = make_discovered_descriptor(scopes=["files.read"])
    resolved = _resolved(challenge_scopes=("x",), scopes_supported=("y",), offline_access_supported=True)
    assert requested_scopes(descriptor, ["files"], resolved) == ["files.read"]


def test_with_none_in_the_descriptor_the_challenge_wins_over_the_document():
    resolved = _resolved(challenge_scopes=("a", "b"), scopes_supported=("c",))
    assert requested_scopes(make_discovered_descriptor(), ["files"], resolved) == ["a", "b"]


def test_the_documents_scopes_supported_when_the_challenge_named_none():
    resolved = _resolved(scopes_supported=("c", "d"))
    assert requested_scopes(make_discovered_descriptor(), ["files"], resolved) == ["c", "d"]


def test_discovered_scopes_gain_offline_access_only_when_the_server_lists_it():
    listed = _resolved(scopes_supported=("c",), offline_access_supported=True)
    assert requested_scopes(make_discovered_descriptor(), ["files"], listed) == ["c", "offline_access"]
    unlisted = _resolved(scopes_supported=("c",), offline_access_supported=False)
    assert requested_scopes(make_discovered_descriptor(), ["files"], unlisted) == ["c"]


def test_no_scopes_anywhere_means_no_scope_at_all():
    resolved = _resolved(offline_access_supported=True)
    assert requested_scopes(make_discovered_descriptor(), ["files"], resolved) == []


def test_a_reconnect_asks_for_the_union_with_the_granted_and_wanted_scopes():
    resolved = _resolved(challenge_scopes=("a",))
    record = _record(granted=["b", "a"], wanted=["c"])
    assert requested_scopes(make_discovered_descriptor(), ["files"], resolved, record=record) == ["a", "b", "c"]


def test_a_declared_reconnect_keeps_earlier_permissions():
    descriptor = make_oauth_descriptor()
    record = _record(granted=["cal.read"], wanted=[])
    scopes = requested_scopes(descriptor, ["mail"], authorization._declared(descriptor), record=record)
    assert scopes == ["mail.read", "mail.send", "cal.read"]


# -- the issuer check on the login redirect -------------------------------------------


@pytest.mark.parametrize(
    ("supported", "iss", "accepted"),
    [
        (True, "https://as.example.com", True),
        (True, "https://other.example.com", False),
        (True, None, False),
        (False, "https://other.example.com", False),
        (False, "https://as.example.com", True),
        (False, None, True),
    ],
)
def test_the_redirect_issuer_table(supported: bool, iss: str | None, accepted: bool):
    def check() -> None:
        check_redirect_issuer(recorded_issuer="https://as.example.com", iss_required=supported, iss=iss)

    if accepted:
        check()
    else:
        with pytest.raises(OAuthIssuerMismatchError):
            check()


@pytest.mark.parametrize("iss", ["https://as.example.com/", "https://AS.example.com", "https://as.example.com:443"])
def test_a_redirect_issuer_that_differs_in_any_way_is_rejected(iss: str):
    with pytest.raises(OAuthIssuerMismatchError):
        check_redirect_issuer(recorded_issuer="https://as.example.com", iss_required=False, iss=iss)


def test_a_login_without_a_recorded_issuer_is_not_checked():
    check_redirect_issuer(recorded_issuer=None, iss_required=False, iss="https://anything.example.com")


def test_the_redirect_error_kinds():
    assert error_kind(OAuthIssuerMismatchError("x")) is ErrorKind.UNAUTHORIZED
    assert error_kind(OAuthAuthorizationCancelledError("x")) is ErrorKind.CANCELLED


# -- grants -------------------------------------------------------------------------


async def test_a_grant_from_a_listed_issuer_is_served_there_with_its_resource(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    grant = ConnectionGrant(issuer=f"{fake.base}/a", resource="https://r.example.com", client_id="c-1")
    target = await resolve_for_grant(make_discovered_descriptor(mcp_url=mcp), grant)
    assert target.resolved.issuer == f"{fake.base}/a"
    assert target.resource == "https://r.example.com"
    assert target.client_id == "c-1"


async def test_an_unpinned_server_that_no_longer_lists_the_grants_issuer(fake: FakeOAuthServer):
    mcp = _two_servers(fake, order=("/b",))
    grant = ConnectionGrant(issuer=f"{fake.base}/a")
    with pytest.raises(GrantUnservableError, match="no longer lists"):
        await resolve_for_grant(make_discovered_descriptor(mcp_url=mcp), grant)


async def test_a_pin_that_names_another_server_than_the_grants(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    grant = ConnectionGrant(issuer=f"{fake.base}/a")
    with pytest.raises(GrantUnservableError, match="pins"):
        await resolve_for_grant(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/b"), grant)


async def test_a_pinned_issuer_the_server_does_not_list_may_still_recover(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    grant = ConnectionGrant(issuer=f"{fake.base}/c")
    with pytest.raises(OAuthDiscoveryError) as info:
        await resolve_for_grant(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/c"), grant)
    assert info.value.step == "issuer-mismatch"


async def test_a_stored_client_grant_under_an_env_client_descriptor_is_decided_before_anything_is_fetched(
    fake: FakeOAuthServer,
):
    mcp = _two_servers(fake)
    grant = ConnectionGrant(issuer=f"{fake.base}/a", client_id="stored-1")
    with pytest.raises(GrantUnservableError, match="stored client"):
        await resolve_for_grant(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"), grant)
    assert fake.received == []


async def test_an_env_client_grant_under_a_declared_descriptor_is_served_the_declared_way():
    grant = ConnectionGrant(issuer="https://as.example.com", resource="https://r.example.com")
    target = await resolve_for_grant(make_oauth_descriptor(), grant)
    assert not target.resolved.guarded
    assert target.resource is None
    assert target.client_id is None


async def test_a_record_with_no_grant_under_a_pinned_descriptor_resolves_at_the_pin(fake: FakeOAuthServer):
    mcp = _two_servers(fake)
    target = await resolve_for_grant(make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"), None)
    assert target.resolved.issuer == f"{fake.base}/a"
    assert target.resource is None
    assert target.client_id is None


def test_grant_for_records_where_and_with_which_client_a_login_was_made():
    discovered = make_discovered_descriptor()
    env = authorization.ResolvedClient(client_id="e", client_secret="s", auth_method="client_secret_post", source="env")
    stored = authorization.ResolvedClient(client_id="d-1", client_secret=None, auth_method="none", source="document")
    assert authorization.grant_for(discovered, _resolved(), env) == ConnectionGrant(
        issuer="https://as.example.com", resource="https://mcp.example.com/mcp", client_id=None
    )
    assert authorization.grant_for(discovered, _resolved(), stored).client_id == "d-1"  # type: ignore[union-attr]
    declared = make_oauth_descriptor()
    assert authorization.grant_for(declared, authorization._declared(declared), env) is None
