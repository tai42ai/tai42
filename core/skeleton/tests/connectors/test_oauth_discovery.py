"""Discovery of a discovered provider's authorization server, against a loopback protocol fixture.

One scenario per shape the shipped MCP servers publish (a path-suffixed pointer with a path issuer, an
origin-level resource, a trailing-slash resource, a server that answers the probe 200), the negatives
of every step, the cache, and the URL guard on every URL a remote names.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tai42_contract.errors import ErrorKind, error_kind
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import discovery
from tai42_skeleton.connectors.oauth.discovery import (
    OAuthDiscoveryError,
    discover,
    forget,
    parse_www_authenticate,
    same_issuer,
)

from .oauth_fakes import FakeOAuthServer, Reply, serve


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


def _prm(fake: FakeOAuthServer, path: str, resource: str, servers: list[str], **extra) -> None:
    fake.json(path, {"resource": resource, "authorization_servers": servers, **extra})


# -- the WWW-Authenticate parser and the issuer comparison --------------------------


def test_parser_reads_quoted_and_bare_parameters_of_a_bearer_challenge():
    challenge = parse_www_authenticate(
        'Bearer realm="x", resource_metadata="https://a/b", scope="r w", error=insufficient_scope'
    )
    assert challenge is not None
    assert challenge.resource_metadata == "https://a/b"
    assert challenge.scope == ("r", "w")
    assert challenge.error == "insufficient_scope"
    bare = parse_www_authenticate("Bearer resource_metadata=https://a/b,scope=one")
    assert bare is not None
    assert bare.resource_metadata == "https://a/b"
    assert bare.scope == ("one",)


def test_parser_finds_the_bearer_challenge_among_several_and_reads_escapes():
    challenge = parse_www_authenticate('Basic realm="a, b", Bearer resource_metadata="https://a/\\"q\\""')
    assert challenge is not None
    assert challenge.resource_metadata == 'https://a/"q"'


@pytest.mark.parametrize("value", [None, "", "Basic realm=x", "Bearer", "Negotiate abc==", "garbage ==="])
def test_parser_any_other_shape_yields_no_candidate(value):
    challenge = parse_www_authenticate(value)
    assert challenge is None or challenge.resource_metadata is None


def test_same_issuer_is_plain_string_equality():
    assert same_issuer("https://a.example.com/x", "https://a.example.com/x")
    assert not same_issuer("https://a.example.com/x", "https://a.example.com/x/")
    assert not same_issuer("https://A.example.com", "https://a.example.com")
    assert not same_issuer("https://a.example.com:443", "https://a.example.com")
    assert not same_issuer("https://a.example.com", None)
    assert same_issuer(None, None)


def test_discovery_error_kind():
    assert error_kind(OAuthDiscoveryError("x", step="challenge")) is ErrorKind.UPSTREAM_ERROR


# -- vendor shapes -------------------------------------------------------------------


async def test_path_suffixed_pointer_and_an_issuer_with_a_path(fake: FakeOAuthServer):
    mcp = f"{fake.base}/v1/mcp/authv2"
    pointer = f"{fake.base}/.well-known/oauth-protected-resource/v1/mcp/authv2"
    fake.protected_mcp("/v1/mcp/authv2", pointer=pointer)
    issuer = f"{fake.base}/tenant-a"
    _prm(fake, "/.well-known/oauth-protected-resource/v1/mcp/authv2", mcp, [issuer], scopes_supported=["read"])
    fake.authorization_server(
        "/tenant-a",
        registration_endpoint=f"{fake.base}/tenant-a/register",
        revocation_endpoint=f"{fake.base}/tenant-a/revoke",
        client_id_metadata_document_supported=True,
        token_endpoint_auth_methods_supported=["client_secret_post", "none"],
        scopes_supported=["read", "offline_access"],
    )
    result = await discover(mcp)
    assert result.issuer == issuer
    assert result.resource == mcp
    assert result.authorization_endpoint == f"{fake.base}/tenant-a/authorize"
    assert result.token_endpoint == f"{fake.base}/tenant-a/token"
    assert result.registration_endpoint == f"{fake.base}/tenant-a/register"
    assert result.revocation_endpoint == f"{fake.base}/tenant-a/revoke"
    assert result.scopes_supported == ("read",)
    assert result.offline_access_supported
    assert result.client_id_document_supported
    assert not result.iss_parameter_supported
    assert result.token_endpoint_auth_methods_supported == ("client_secret_post", "none")
    assert result.authorization_servers == (issuer,)
    # RFC 8414 path insertion found the metadata; the OIDC candidates were never needed.
    assert fake.requests_to("/.well-known/openid-configuration/tenant-a") == []


async def test_origin_level_resource_for_a_path_endpoint(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/.well-known/oauth-protected-resource")
    _prm(fake, "/.well-known/oauth-protected-resource", fake.base, [fake.base])
    fake.authorization_server("", token_endpoint_auth_methods_supported=["client_secret_post"])
    result = await discover(mcp)
    assert result.resource == fake.base
    assert result.issuer == fake.base
    assert result.registration_endpoint is None
    assert result.revocation_endpoint is None
    assert result.challenge_scopes is None
    assert result.scopes_supported == ()


@pytest.mark.parametrize(("typed", "published"), [("/mcp", "/mcp/"), ("/mcp/", "/mcp")])
async def test_one_trailing_slash_is_ignored_and_the_published_resource_is_returned(
    fake: FakeOAuthServer, typed: str, published: str
):
    mcp = f"{fake.base}{typed}"
    fake.protected_mcp(typed, pointer=f"{fake.base}/prm")
    issuer = f"{fake.base}/login/oauth"
    _prm(fake, "/prm", f"{fake.base}{published}", [issuer])
    fake.authorization_server("/login/oauth", authorization_response_iss_parameter_supported=True)
    result = await discover(mcp)
    assert result.resource == f"{fake.base}{published}"
    assert result.iss_parameter_supported


async def test_a_200_on_initialize_with_the_document_only_at_the_path_suffixed_url(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp/v1"
    fake.route(
        "POST", "/mcp/v1", Reply(status=200, body={"jsonrpc": "2.0", "id": 1}, headers={"Mcp-Session-Id": "s-9"})
    )
    fake.route("DELETE", "/mcp/v1", Reply(status=204))
    _prm(fake, "/.well-known/oauth-protected-resource/mcp/v1", mcp, [fake.base])
    fake.authorization_server("")
    result = await discover(mcp)
    assert result.resource == mcp
    probe = fake.requests_to("/mcp/v1", "POST")[0]
    assert probe.json["method"] == "initialize"
    assert "text/event-stream" in probe.headers["accept"]
    assert "authorization" not in probe.headers
    # The session the probe opened is closed with its id.
    (closed,) = fake.requests_to("/mcp/v1", "DELETE")
    assert closed.headers["mcp-session-id"] == "s-9"


async def test_the_challenge_scope_is_kept(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm", scope="a b")
    _prm(fake, "/prm", mcp, [fake.base], scopes_supported=["c"])
    fake.authorization_server("")
    result = await discover(mcp)
    assert result.challenge_scopes == ("a", "b")
    assert result.scopes_supported == ("c",)


async def test_an_openid_document_only(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=None)
    issuer = f"{fake.base}/realm"
    _prm(fake, "/.well-known/oauth-protected-resource/mcp", mcp, [issuer])
    fake.authorization_server("/realm", well_known="/realm/.well-known/openid-configuration")
    result = await discover(mcp)
    assert result.token_endpoint == f"{fake.base}/realm/token"
    tried = [
        r.path for r in fake.received if r.method == "GET" and ("authorization-server" in r.path or "openid" in r.path)
    ]
    assert tried == [
        "/.well-known/oauth-authorization-server/realm",
        "/.well-known/openid-configuration/realm",
        "/realm/.well-known/openid-configuration",
    ]


async def test_a_resource_document_with_the_name_returns_it(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base], resource_name="Files")
    fake.authorization_server("")
    assert (await discover(mcp)).resource_name == "Files"


# -- negatives -------------------------------------------------------------------------


async def test_metadata_whose_issuer_differs_by_one_trailing_slash_is_rejected(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    issuer = f"{fake.base}/as"
    _prm(fake, "/prm", mcp, [issuer])
    fake.authorization_server("/as", issuer=f"{issuer}/")
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "issuer-mismatch"


@pytest.mark.parametrize("pkce", [None, ["plain"]])
async def test_metadata_without_s256_is_rejected(fake: FakeOAuthServer, pkce):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    metadata = fake.authorization_server("", pkce=pkce or [])
    if pkce is None:
        metadata.pop("code_challenge_methods_supported")
        fake.json("/.well-known/oauth-authorization-server", metadata)
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "pkce-unsupported"


async def test_no_protected_resource_document(fake: FakeOAuthServer):
    fake.protected_mcp("/mcp", pointer=None)
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(f"{fake.base}/mcp")
    assert info.value.step == "resource-metadata"


async def test_a_resource_for_another_host_is_a_mismatch(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", "https://other.example.com/mcp", [fake.base])
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "resource-mismatch"


async def test_a_sibling_path_is_not_a_parent(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", f"{fake.base}/mc", [fake.base])
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "resource-mismatch"


async def test_a_bound_issuer_the_server_does_not_list(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    fake.authorization_server("")
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp, bound_issuer=f"{fake.base}/")
    assert info.value.step == "issuer-mismatch"


async def test_an_http_token_endpoint_on_a_non_loopback_host_fails_the_step(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    fake.authorization_server("", token_endpoint="http://auth.example.com/token")
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "authorization-server-metadata"


async def test_metadata_without_a_token_endpoint_fails_the_step(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    metadata = fake.authorization_server("")
    metadata.pop("token_endpoint")
    fake.json("/.well-known/oauth-authorization-server", metadata)
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "authorization-server-metadata"


async def test_no_authorization_server_metadata(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "authorization-server-metadata"


async def test_the_resource_string_is_returned_byte_identical(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    published = f"{fake.base}/mcp/"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", published, [fake.base])
    fake.authorization_server("")
    assert (await discover(mcp)).resource == published


async def test_an_unreachable_mcp_server_fails_the_challenge(fake: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(f"http://127.0.0.1:{port}/mcp")
    assert info.value.step == "challenge"


# -- the cache -------------------------------------------------------------------------


async def test_a_cache_hit_within_the_ttl_and_fresh_refetches(fake: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    fake.authorization_server("")
    clock = [1000.0]
    monkeypatch.setattr(discovery, "_monotonic", lambda: clock[0])
    await discover(mcp)
    await discover(mcp)
    assert len(fake.requests_to("/prm")) == 1
    assert len(fake.requests_to("/.well-known/oauth-authorization-server")) == 1
    await discover(mcp, fresh=True)
    assert len(fake.requests_to("/prm")) == 2
    assert len(fake.requests_to("/.well-known/oauth-authorization-server")) == 2
    clock[0] += discovery.CACHE_TTL_SECONDS + 1
    await discover(mcp)
    assert len(fake.requests_to("/prm")) == 3


async def test_a_bound_call_after_an_unbound_one_returns_its_own_server(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    server_a = f"{fake.base}/a"
    server_b = f"{fake.base}/b"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [server_b, server_a])
    fake.authorization_server("/a")
    fake.authorization_server("/b")
    unbound = await discover(mcp)
    bound = await discover(mcp, bound_issuer=server_a)
    again = await discover(mcp)
    assert unbound.issuer == server_b
    assert unbound.token_endpoint == f"{server_b}/token"
    assert bound.issuer == server_a
    assert bound.token_endpoint == f"{server_a}/token"
    assert again.issuer == server_b
    assert len(fake.requests_to("/prm")) == 1
    assert len(fake.requests_to("/.well-known/oauth-authorization-server/a")) == 1
    assert len(fake.requests_to("/.well-known/oauth-authorization-server/b")) == 1


async def test_a_failed_fetch_is_not_cached(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.route(
        "GET",
        "/prm",
        [Reply(status=500), Reply(status=200, body={"resource": mcp, "authorization_servers": [fake.base]})],
    )
    fake.authorization_server("")
    with pytest.raises(OAuthDiscoveryError):
        await discover(mcp)
    assert (await discover(mcp)).resource == mcp


async def test_a_document_that_fails_its_checks_is_not_cached(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.route(
        "GET",
        "/prm",
        [
            Reply(status=200, body={"resource": "https://other.example.com", "authorization_servers": [fake.base]}),
            Reply(status=200, body={"resource": mcp, "authorization_servers": [fake.base]}),
        ],
    )
    # The path-suffixed and root candidates are absent (404), so the first call ends in a mismatch.
    fake.authorization_server("")
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "resource-mismatch"
    assert (await discover(mcp)).resource == mcp


async def test_forget_drops_the_document_and_its_servers_metadata(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    fake.authorization_server("")
    await discover(mcp)
    forget(mcp)
    await discover(mcp)
    assert len(fake.requests_to("/prm")) == 2
    assert len(fake.requests_to("/.well-known/oauth-authorization-server")) == 2


# -- the guard on every URL a remote names -------------------------------------------


async def test_a_resource_metadata_pointer_to_a_private_address_is_refused(fake: FakeOAuthServer):
    fake.protected_mcp("/mcp", pointer="https://10.255.0.7/prm")
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(f"{fake.base}/mcp")
    assert info.value.step == "url-refused"
    assert fake.requests_to("/.well-known/oauth-protected-resource/mcp") == []


async def test_an_authorization_server_at_a_link_local_address_is_refused(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, ["https://169.254.10.10/as"])
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "url-refused"


async def test_an_mcp_url_the_guard_refuses(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover("https://10.1.2.3/mcp")
    assert info.value.step == "url-refused"


async def test_metadata_with_a_flag_that_is_not_a_boolean_is_refused(fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    _prm(fake, "/prm", mcp, [fake.base])
    fake.authorization_server("", authorization_response_iss_parameter_supported="yes")
    with pytest.raises(OAuthDiscoveryError) as info:
        await discover(mcp)
    assert info.value.step == "authorization-server-metadata"
