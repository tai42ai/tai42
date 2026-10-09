"""Dynamic client registration against a loopback protocol fixture (an RFC 7591 registration endpoint).

The request body field by field, the answers accepted and refused, the URL guard on the endpoint, and no
log record carrying a returned secret.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from tai42_contract.errors import ErrorKind, error_kind
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth.authorization import ResolvedAuthorization
from tai42_skeleton.connectors.oauth.registration import (
    ClientRegistrationError,
    application_type,
    preferred_auth_method,
    register_client,
)

from .conftest import make_discovered_descriptor
from .oauth_fakes import FakeOAuthServer, Reply, serve

CALLBACK = "https://app.example.com/oauth-bridge.html"


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    reset_all_settings()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


def _resolved(endpoint: str | None, *, methods: tuple[str, ...] | None = None) -> ResolvedAuthorization:
    return ResolvedAuthorization(
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        revocation_endpoint=None,
        registration_endpoint=endpoint,
        issuer="https://as.example.com",
        resource="https://mcp.example.com/mcp",
        challenge_scopes=None,
        scopes_supported=(),
        offline_access_supported=False,
        token_endpoint_auth_methods_supported=methods,
        client_id_document_supported=False,
        iss_parameter_supported=False,
        guarded=True,
    )


async def _register(fake: FakeOAuthServer, reply: Reply, **kwargs):
    fake.route("POST", "/register", reply)
    resolved = _resolved(f"{fake.base}/register", methods=kwargs.pop("methods", None))
    return await register_client(
        make_discovered_descriptor(),
        resolved,
        kwargs.pop("redirect_uri", CALLBACK),
        scopes=kwargs.pop("scopes", ["files.read", "offline_access"]),
    )


async def test_the_request_body_field_by_field(fake: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTORS_CLIENT_NAME", "Acme Assistant")
    reset_all_settings()
    await _register(fake, Reply(status=201, body={"client_id": "c1", "client_secret": "s1"}))
    (request,) = fake.requests_to("/register", "POST")
    assert request.headers["content-type"].startswith("application/json")
    assert request.json == {
        "redirect_uris": [CALLBACK],
        "application_type": "web",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_basic",
        "client_name": "Acme Assistant",
        "scope": "files.read offline_access",
    }


async def test_no_scope_is_sent_when_there_is_none(fake: FakeOAuthServer):
    await _register(fake, Reply(status=201, body={"client_id": "c1", "client_secret": "s1"}), scopes=[])
    (request,) = fake.requests_to("/register", "POST")
    assert "scope" not in request.json
    assert request.json["client_name"] == "Tai"


async def test_a_loopback_redirect_uri_registers_a_native_client(fake: FakeOAuthServer):
    await _register(
        fake,
        Reply(status=201, body={"client_id": "c1", "client_secret": "s1"}),
        redirect_uri="http://localhost:5173/oauth-bridge.html",
    )
    (request,) = fake.requests_to("/register", "POST")
    assert request.json["application_type"] == "native"
    assert request.json["redirect_uris"] == ["http://localhost:5173/oauth-bridge.html"]


@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        ("http://127.0.0.1:8000/cb", "native"),
        ("http://[::1]:8000/cb", "native"),
        ("http://localhost/cb", "native"),
        ("https://app.example.com/cb", "web"),
        ("https://10.1.2.3/cb", "web"),
    ],
)
def test_application_type(redirect_uri: str, expected: str):
    assert application_type(redirect_uri) == expected


@pytest.mark.parametrize(
    ("advertised", "expected"),
    [
        (None, "client_secret_basic"),
        (("client_secret_post", "client_secret_basic", "none"), "client_secret_post"),
        (("none", "client_secret_basic"), "client_secret_basic"),
        (("private_key_jwt", "none"), "none"),
    ],
)
def test_the_method_preference_for_each_advertised_list(advertised, expected):
    assert preferred_auth_method(advertised) == expected


def test_a_list_with_no_method_the_engine_speaks_is_refused():
    with pytest.raises(ClientRegistrationError, match="private_key_jwt"):
        preferred_auth_method(("private_key_jwt",))


async def test_the_advertised_preference_is_sent(fake: FakeOAuthServer):
    await _register(
        fake, Reply(status=201, body={"client_id": "c1", "client_secret": "s1"}), methods=("none", "client_secret_post")
    )
    (request,) = fake.requests_to("/register", "POST")
    assert request.json["token_endpoint_auth_method"] == "client_secret_post"


@pytest.mark.parametrize("status", [200, 201])
async def test_a_200_or_201_answer_is_accepted(fake: FakeOAuthServer, status: int):
    registered = await _register(
        fake,
        Reply(
            status=status,
            body={
                "client_id": "c1",
                "client_secret": "s1",
                "token_endpoint_auth_method": "client_secret_post",
                "client_secret_expires_at": 1893456000,
            },
        ),
    )
    assert registered.client_id == "c1"
    assert registered.client_secret == "s1"
    assert registered.auth_method == "client_secret_post"
    assert registered.secret_expires_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert registered.redirect_uri == CALLBACK


async def test_defaults_of_an_answer_that_names_no_method_and_no_expiry(fake: FakeOAuthServer):
    registered = await _register(fake, Reply(status=201, body={"client_id": "c1", "client_secret": "s1"}))
    assert registered.auth_method == "client_secret_basic"
    assert registered.secret_expires_at is None


async def test_an_expiry_of_zero_never_expires_and_a_public_client_has_no_secret(fake: FakeOAuthServer):
    registered = await _register(
        fake,
        Reply(
            status=201, body={"client_id": "c1", "token_endpoint_auth_method": "none", "client_secret_expires_at": 0}
        ),
    )
    assert registered.client_secret is None
    assert registered.auth_method == "none"
    assert registered.secret_expires_at is None


async def test_a_refusal_raises_with_the_servers_code(fake: FakeOAuthServer):
    long = "x" * 500
    with pytest.raises(ClientRegistrationError) as info:
        await _register(fake, Reply(status=400, body={"error": "invalid_redirect_uri", "error_description": long}))
    assert info.value.status == 400
    assert info.value.error_code == "invalid_redirect_uri"
    assert long not in str(info.value)
    assert error_kind(info.value) is ErrorKind.UPSTREAM_ERROR


async def test_a_non_json_refusal_raises_with_its_status(fake: FakeOAuthServer):
    with pytest.raises(ClientRegistrationError) as info:
        await _register(fake, Reply(status=503, raw=b"<html>down</html>"))
    assert (info.value.status, info.value.error_code) == (503, None)


@pytest.mark.parametrize(
    "body",
    [
        {"client_secret": "s1"},
        {"client_id": ""},
        {"client_id": 7},
        {"client_id": "c1", "client_secret": 9},
        {"client_id": "c1", "client_secret": "s", "client_secret_expires_at": "soon"},
        {"client_id": "c1", "client_secret": "s", "client_secret_expires_at": -1},
        {"client_id": "c1", "client_secret": "s", "client_secret_expires_at": True},
        {"client_id": "c1"},
        ["c1"],
    ],
    ids=[
        "no-id",
        "empty-id",
        "non-string-id",
        "non-string-secret",
        "non-integer-expiry",
        "negative-expiry",
        "boolean-expiry",
        "secret-method-without-secret",
        "not-an-object",
    ],
)
async def test_an_unusable_answer_raises(fake: FakeOAuthServer, body):
    with pytest.raises(ClientRegistrationError):
        await _register(fake, Reply(status=201, body=body))


async def test_an_unsupported_returned_method_raises(fake: FakeOAuthServer):
    with pytest.raises(ClientRegistrationError, match="private_key_jwt"):
        await _register(
            fake,
            Reply(status=201, body={"client_id": "c1", "token_endpoint_auth_method": "private_key_jwt"}),
        )


async def test_a_private_registration_endpoint_is_refused_before_any_connection(fake: FakeOAuthServer):
    with pytest.raises(ClientRegistrationError, match="refused"):
        await register_client(make_discovered_descriptor(), _resolved("https://10.0.0.5/register"), CALLBACK, scopes=[])
    assert fake.received == []


async def test_a_server_offering_no_registration_raises():
    with pytest.raises(ClientRegistrationError):
        await register_client(make_discovered_descriptor(), _resolved(None), CALLBACK, scopes=[])


async def test_a_transport_failure_raises(fake: FakeOAuthServer):
    endpoint = f"{fake.base}/register"
    fake._server.shutdown()
    fake._server.server_close()
    with pytest.raises(ClientRegistrationError, match="transport"):
        await register_client(make_discovered_descriptor(), _resolved(endpoint), CALLBACK, scopes=[])


async def test_no_log_record_of_a_successful_registration_carries_the_secret(
    fake: FakeOAuthServer, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)
    await _register(fake, Reply(status=201, body={"client_id": "c1", "client_secret": "returned-secret-value"}))
    assert caplog.records
    for record in caplog.records:
        assert "returned-secret-value" not in record.getMessage()
        assert "returned-secret-value" not in str(record.args)
