"""OAuth client: PKCE, redirect allow-list, authorize URL, exchange/refresh/revoke."""

from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.oauth.client as client_mod
from tai42_skeleton.connectors.oauth import authorization, client
from tai42_skeleton.connectors.oauth.client import (
    CodeExchangeFailedError,
    RedirectUriNotAllowedError,
    RefreshTokenMissingError,
    TokenRefreshFailedError,
)

from .conftest import (
    ENV_CLIENT,
    FakeHttp,
    FakeHttpResponse,
    declared_call,
    make_discovered_descriptor,
    make_oauth_descriptor,
)
from .oauth_fakes import FakeOAuthServer, Reply, serve


@pytest.fixture
def install_http(monkeypatch):
    """Install a queued fake pooled-HTTP client; return the FakeHttp for asserts."""

    def _install(responses):
        fake = FakeHttp(responses)

        @asynccontextmanager
        async def fake_http():
            yield fake

        monkeypatch.setattr(client_mod, "_http", fake_http)
        return fake

    return _install


# -- PKCE --------------------------------------------------------------------


def test_generate_pkce_pair_is_valid_s256():
    verifier, challenge = client.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected
    # fresh each call
    assert client.generate_pkce_pair()[0] != verifier


# -- Redirect allow-list -----------------------------------------------------


def test_validate_redirect_uri_accepts_https_in_allowlist():
    out = client.validate_redirect_uri("https://app.example.com/oauth-bridge.html")
    assert out == "https://app.example.com/oauth-bridge.html"


def test_validate_redirect_uri_accepts_localhost_http():
    out = client.validate_redirect_uri("http://localhost:5173/oauth-bridge.html")
    assert out == "http://localhost:5173/oauth-bridge.html"


def test_validate_redirect_uri_returns_uri_unchanged():
    # No normalization: OAuth requires the redirect_uri to be byte-identical
    # across authorize and token exchange, so a trailing slash is preserved.
    assert client.validate_redirect_uri("https://app.example.com/") == "https://app.example.com/"


def test_validate_redirect_uri_rejects_empty():
    with pytest.raises(RedirectUriNotAllowedError, match="non-empty"):
        client.validate_redirect_uri("")


def test_validate_redirect_uri_rejects_unparseable():
    with pytest.raises(RedirectUriNotAllowedError, match="scheme and host"):
        client.validate_redirect_uri("notaurl")


def test_validate_redirect_uri_rejects_http_for_non_local():
    with pytest.raises(RedirectUriNotAllowedError, match="https"):
        client.validate_redirect_uri("http://app.example.com/cb")


def test_validate_redirect_uri_rejects_origin_not_in_allowlist():
    with pytest.raises(RedirectUriNotAllowedError, match="not in the allow-list"):
        client.validate_redirect_uri("https://evil.example.com/cb")


def test_validate_redirect_uri_rejects_unallowlisted_bridge_origin():
    # A configured OAuth bridge origin must ALSO appear in
    # CONNECTORS_REDIRECT_URI_ALLOWLIST, or every Connect flow bounced through it
    # fails closed here at the token exchange.
    with pytest.raises(RedirectUriNotAllowedError, match="not in the allow-list"):
        client.validate_redirect_uri("https://bridge.tai42.ai/oauth-bridge.html")


def test_validate_redirect_uri_rejects_empty_allowlist(monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", "")
    reset_all_settings()
    with pytest.raises(RedirectUriNotAllowedError, match="allow-list"):
        client.validate_redirect_uri("https://app.example.com/cb")


# -- Authorize URL -----------------------------------------------------------


def test_build_authorize_url(oauth_client_env):
    desc = make_oauth_descriptor(extra_authorize_params={"access_type": "offline"})
    url = client.build_authorize_url(
        **declared_call(desc),
        scopes=["mail.read", "mail.send"],
        state="signed-state",
        code_challenge="chal",
        redirect_uri="https://app.example.com/oauth-bridge.html",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "acme.test"
    q = parse_qs(parsed.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["client-123"]
    assert q["scope"] == ["mail.read mail.send"]
    assert q["state"] == ["signed-state"]
    assert q["code_challenge"] == ["chal"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"]


def test_build_authorize_url_enforces_allowlist(oauth_client_env):
    desc = make_oauth_descriptor()
    with pytest.raises(RedirectUriNotAllowedError):
        client.build_authorize_url(
            **declared_call(desc),
            scopes=["mail.read"],
            state="s",
            code_challenge="c",
            redirect_uri="https://evil.example.com/cb",
        )


# -- _error_detail -----------------------------------------------------------


def test_error_detail_non_json():
    # FakeHttpResponse is a minimal stand-in; httpx.Response is a concrete class,
    # so pyright cannot accept it structurally — cast at the _error_detail seam.
    resp = FakeHttpResponse(status_code=500, raise_on_json=True, content=b"oops")
    assert "non_json" in client._error_detail(cast(httpx.Response, resp))


def test_error_detail_non_object():
    resp = FakeHttpResponse(json_body=[1, 2], content=b"[1,2]")
    assert "non_object" in client._error_detail(cast(httpx.Response, resp))


def test_error_detail_truncates_long_description():
    long_desc = "x" * 200
    resp = FakeHttpResponse(json_body={"error": "bad", "error_description": long_desc})
    detail = client._error_detail(cast(httpx.Response, resp))
    assert "bad" in detail
    assert "…" in detail


def test_error_detail_error_only():
    resp = FakeHttpResponse(json_body={"error": "bad"})
    assert client._error_detail(cast(httpx.Response, resp)) == "error='bad'"


def test_error_detail_no_error_field():
    resp = FakeHttpResponse(json_body={"other": 1})
    assert client._error_detail(cast(httpx.Response, resp)) == "no_error_field"


# -- exchange_code -----------------------------------------------------------


async def test_exchange_code_success(oauth_client_env, install_http):
    install_http(
        [
            FakeHttpResponse(
                json_body={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 7200,
                    "scope": "mail.read mail.send",
                }
            ),
        ]
    )
    desc = make_oauth_descriptor()
    resp = await client.exchange_code(
        **declared_call(desc),
        code="auth-code",
        code_verifier="v",
        redirect_uri="https://app.example.com/cb",
    )
    assert resp.access_token == "at"
    assert resp.refresh_token == "rt"
    assert resp.granted_scopes == ["mail.read", "mail.send"]


async def test_exchange_code_sends_accept_json_header(oauth_client_env, install_http):
    # RFC 6749 §5.1: the token POST asks for a JSON response so a server that would
    # otherwise default to form-encoding returns parseable JSON.
    fake = install_http([FakeHttpResponse(json_body={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})])
    await client.exchange_code(
        **declared_call(),
        code="c",
        code_verifier="v",
        redirect_uri="https://app.example.com/cb",
    )
    assert fake.headers[0].get("Accept") == "application/json"


async def test_exchange_code_200_without_access_token_names_provider_error(oauth_client_env, install_http):
    # A vendor that signals failure in a 200 body carrying no access_token is loud with
    # its RFC 6749 error / error_description reason.
    install_http(
        [FakeHttpResponse(status_code=200, json_body={"ok": False, "error": "x", "error_description": "bad thing"})]
    )
    with pytest.raises(CodeExchangeFailedError, match="x") as exc:
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )
    message = str(exc.value)
    assert "x" in message
    assert "bad thing" in message


async def test_exchange_code_transport_error(oauth_client_env, install_http):
    install_http([httpx.ConnectError("boom")])
    with pytest.raises(CodeExchangeFailedError, match="transport error"):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_non_200(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=400, json_body={"error": "invalid_grant"})])
    with pytest.raises(CodeExchangeFailedError, match="status 400"):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_non_json_200(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=200, raise_on_json=True, content=b"notjson")])
    with pytest.raises(CodeExchangeFailedError, match="non-JSON"):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_missing_refresh_token(oauth_client_env, install_http):
    install_http([FakeHttpResponse(json_body={"access_token": "at", "expires_in": 3600})])
    with pytest.raises(RefreshTokenMissingError):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_missing_access_token(oauth_client_env, install_http):
    install_http([FakeHttpResponse(json_body={"expires_in": 3600})])
    with pytest.raises(CodeExchangeFailedError, match="missing access_token"):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_non_string_access_token_raises(oauth_client_env, install_http):
    # A non-string access_token is a malformed body, classified rather than
    # escaping as a raw 500.
    install_http([FakeHttpResponse(json_body={"access_token": 12345, "refresh_token": "rt", "expires_in": 3600})])
    with pytest.raises(CodeExchangeFailedError, match="non-string access_token"):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_list_scope_raises_classified(oauth_client_env, install_http):
    # A list-typed scope would AttributeError on ``.split``; it must surface as the
    # documented CodeExchangeFailedError instead.
    install_http(
        [
            FakeHttpResponse(
                json_body={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": ["mail.read"]}
            )
        ]
    )
    with pytest.raises(CodeExchangeFailedError, match="non-string scope"):
        await client.exchange_code(
            **declared_call(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_exchange_code_allows_missing_refresh_when_not_required(oauth_client_env, install_http):
    install_http([FakeHttpResponse(json_body={"access_token": "at", "expires_in": 3600})])
    resp = await client.exchange_code(
        **declared_call(),
        code="c",
        code_verifier="v",
        redirect_uri="https://app.example.com/cb",
        require_refresh_token=False,
    )
    assert resp.refresh_token is None


# -- refresh -----------------------------------------------------------------


async def test_refresh_success(oauth_client_env, install_http):
    fake = install_http(
        [
            FakeHttpResponse(
                json_body={
                    "access_token": "new-at",
                    "refresh_token": "new-rt",
                    "expires_in": 3600,
                    "scope": "mail.read",
                }
            )
        ]
    )
    resp = await client.refresh(**declared_call(), refresh_token="old-rt")
    assert resp.access_token == "new-at"
    assert resp.refresh_token == "new-rt"
    # RFC 6749 §5.1: the refresh token POST also asks for a JSON response.
    assert fake.headers[0].get("Accept") == "application/json"


async def test_refresh_keeps_old_refresh_token_when_omitted(oauth_client_env, install_http):
    install_http([FakeHttpResponse(json_body={"access_token": "new-at", "expires_in": 3600})])
    resp = await client.refresh(**declared_call(), refresh_token="old-rt")
    assert resp.refresh_token == "old-rt"


async def test_refresh_transport_error_is_transient(oauth_client_env, install_http):
    install_http([httpx.ConnectError("boom")])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "transient"


async def test_refresh_5xx_is_transient(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=503)])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "transient"
    assert ei.value.http_status == 503


async def test_refresh_invalid_grant_is_terminal(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=400, json_body={"error": "invalid_grant"})])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "invalid_grant"


async def test_refresh_other_4xx_is_transient(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=429, json_body={"error": "slow_down"})])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "transient"


async def test_refresh_non_json_4xx_is_transient(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=400, raise_on_json=True, content=b"x")])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "transient"


async def test_refresh_non_json_200_is_transient(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=200, raise_on_json=True, content=b"x")])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "transient"


async def test_refresh_list_scope_is_reclassified_transient(oauth_client_env, install_http):
    # The shared parser raises CodeExchangeFailedError on a malformed 200 body;
    # refresh re-maps it to its own contract so it never escapes unclassified.
    install_http([FakeHttpResponse(json_body={"access_token": "at", "expires_in": 3600, "scope": ["mail.read"]})])
    with pytest.raises(TokenRefreshFailedError) as ei:
        await client.refresh(**declared_call(), refresh_token="rt")
    assert ei.value.reason == "transient"


# -- revoke ------------------------------------------------------------------


async def test_revoke_skipped_when_no_endpoint(oauth_client_env):
    desc = make_oauth_descriptor(revoke=None)
    outcome = await client.revoke(**declared_call(desc), token="t")
    assert outcome.outcome == "skipped"


async def test_revoke_success(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=200)])
    outcome = await client.revoke(**declared_call(), token="t")
    assert outcome.outcome == "success"
    assert outcome.http_status == 200


async def test_revoke_non_2xx_is_failed(oauth_client_env, install_http):
    install_http([FakeHttpResponse(status_code=400, json_body={"error": "bad"})])
    outcome = await client.revoke(**declared_call(), token="t")
    assert outcome.outcome == "failed"
    assert outcome.http_status == 400


async def test_revoke_transport_error_is_failed(oauth_client_env, install_http):
    install_http([httpx.ConnectError("boom")])
    outcome = await client.revoke(**declared_call(), token="t")
    assert outcome.outcome == "failed"


# -- resolved endpoints, client authentication, resource, expiry -----------------------


def _guarded(token_endpoint: str = "https://as.example.com/token", **overrides) -> authorization.ResolvedAuthorization:
    base = {
        "authorization_endpoint": "https://as.example.com/authorize",
        "token_endpoint": token_endpoint,
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
    return authorization.ResolvedAuthorization(**base)


def _client(method: str, *, source: str = "manual", secret: str | None = "s3cret") -> authorization.ResolvedClient:
    return authorization.ResolvedClient(client_id="c id", client_secret=secret, auth_method=method, source=source)  # type: ignore[arg-type]


@pytest.fixture
def fake():
    yield from serve()


@pytest.fixture
def allow_loopback(monkeypatch):
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    reset_all_settings()


def test_authorize_url_carries_resource_and_omits_an_empty_scope():
    url = client.build_authorize_url(
        descriptor=make_discovered_descriptor(),
        resolved=_guarded(),
        client=ENV_CLIENT,
        scopes=[],
        state="s",
        code_challenge="c",
        redirect_uri="https://app.example.com/oauth-bridge.html",
    )
    q = parse_qs(urlparse(url).query)
    assert q["resource"] == ["https://mcp.example.com/mcp"]
    assert "scope" not in q
    assert url.startswith("https://as.example.com/authorize?")


def test_a_declared_authorize_url_carries_no_resource():
    url = client.build_authorize_url(
        **declared_call(),
        scopes=["mail.read"],
        state="s",
        code_challenge="c",
        redirect_uri="https://app.example.com/oauth-bridge.html",
    )
    q = parse_qs(urlparse(url).query)
    assert "resource" not in q
    assert q["scope"] == ["mail.read"]


def test_an_authorization_endpoint_keeps_its_own_query():
    url = client.build_authorize_url(
        descriptor=make_discovered_descriptor(),
        resolved=_guarded(authorization_endpoint="https://as.example.com/authorize?tenant=t1"),
        client=ENV_CLIENT,
        scopes=["a"],
        state="s",
        code_challenge="c",
        redirect_uri="https://app.example.com/oauth-bridge.html",
    )
    q = parse_qs(urlparse(url).query)
    assert q["tenant"] == ["t1"]
    assert q["state"] == ["s"]


@pytest.mark.parametrize(
    ("method", "form_secret", "basic"),
    [
        ("client_secret_post", "s3cret", None),
        ("client_secret_basic", None, "Basic " + base64.b64encode(b"c+id:s3cret").decode()),
        ("none", None, None),
    ],
)
async def test_the_three_client_authentication_shapes_on_exchange_and_refresh(
    install_http, method: str, form_secret: str | None, basic: str | None
):
    fake_http = install_http(
        [
            FakeHttpResponse(json_body={"access_token": "at", "refresh_token": "rt", "expires_in": 60}),
            FakeHttpResponse(json_body={"access_token": "at2", "expires_in": 60}),
        ]
    )
    desc = make_oauth_descriptor()
    resolved = authorization._declared(desc)
    resolved_client = _client(method, secret=None if method == "none" else "s3cret")
    await client.exchange_code(
        descriptor=desc,
        resolved=resolved,
        client=resolved_client,
        code="c",
        code_verifier="v",
        redirect_uri="https://app.example.com/cb",
    )
    await client.refresh(descriptor=desc, resolved=resolved, client=resolved_client, refresh_token="rt")
    for (_, form), headers in zip(fake_http.posts, fake_http.headers, strict=True):
        assert form["client_id"] == "c id"
        assert form.get("client_secret") == form_secret
        assert headers.get("Authorization") == basic


async def test_resource_is_sent_on_exchange_and_refresh(install_http, oauth_client_env):
    fake_http = install_http(
        [
            FakeHttpResponse(json_body={"access_token": "at", "refresh_token": "rt", "expires_in": 60}),
            FakeHttpResponse(json_body={"access_token": "at2", "expires_in": 60}),
        ]
    )
    await client.exchange_code(
        **declared_call(),
        code="c",
        code_verifier="v",
        redirect_uri="https://app.example.com/cb",
        resource="https://r.example.com",
    )
    await client.refresh(**declared_call(), refresh_token="rt", resource="https://r.example.com")
    assert [form["resource"] for _, form in fake_http.posts] == ["https://r.example.com", "https://r.example.com"]


async def test_no_resource_is_sent_without_one(install_http, oauth_client_env):
    fake_http = install_http([FakeHttpResponse(json_body={"access_token": "at", "expires_in": 60})])
    await client.refresh(**declared_call(), refresh_token="rt")
    assert "resource" not in fake_http.posts[0][1]


def test_expiry_of_a_guarded_answer():
    with_expiry = client._parse_token_response({"access_token": "a", "expires_in": 120}, guarded=True)
    assert with_expiry.expires_at is not None
    assert 100 < (with_expiry.expires_at - datetime.now(UTC)).total_seconds() <= 120
    with_refresh = client._parse_token_response({"access_token": "a", "refresh_token": "r"}, guarded=True)
    assert with_refresh.expires_at is not None
    assert 3500 < (with_refresh.expires_at - datetime.now(UTC)).total_seconds() <= 3600
    inherited = client._parse_token_response({"access_token": "a"}, fallback_refresh_token="old", guarded=True)
    assert inherited.expires_at is not None
    neither = client._parse_token_response({"access_token": "a"}, guarded=True)
    assert neither.expires_at is None


def test_a_declared_answer_without_expires_in_assumes_an_hour():
    parsed = client._parse_token_response({"access_token": "a"})
    assert parsed.expires_at is not None
    assert 3500 < (parsed.expires_at - datetime.now(UTC)).total_seconds() <= 3600


def test_a_token_type_of_user_is_accepted():
    assert client._parse_token_response({"access_token": "a", "token_type": "user"}, guarded=True).access_token == "a"


@pytest.mark.parametrize(
    ("source", "reason"),
    [("dynamic", "invalid_client"), ("env", "transient"), ("manual", "transient"), ("document", "transient")],
)
async def test_invalid_client_is_terminal_only_for_a_dynamic_client(install_http, source: str, reason: str):
    install_http([FakeHttpResponse(status_code=401, json_body={"error": "invalid_client"})])
    desc = make_oauth_descriptor()
    with pytest.raises(TokenRefreshFailedError) as info:
        await client.refresh(
            descriptor=desc,
            resolved=authorization._declared(desc),
            client=_client("client_secret_post", source=source),
            refresh_token="rt",
        )
    assert info.value.reason == reason
    assert info.value.error_code == "invalid_client"


async def test_a_non_200_exchange_keeps_the_providers_error_code(install_http, oauth_client_env):
    install_http([FakeHttpResponse(status_code=400, json_body={"error": "invalid_grant"})])
    with pytest.raises(CodeExchangeFailedError) as info:
        await client.exchange_code(
            **declared_call(), code="c", code_verifier="v", redirect_uri="https://app.example.com/cb"
        )
    assert info.value.error_code == "invalid_grant"


def _drip_server():
    import http.server
    import threading
    import time

    class _Drip(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"access_token": "at", "expires_in": 3600}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for byte in body:
                time.sleep(0.1)
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except OSError:
                    return

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


async def test_a_declared_token_endpoint_that_drips_ends_at_the_total_deadline(monkeypatch, oauth_client_env):
    import time

    from tai42_kit.clients.base import shutdown_all_clients

    server = _drip_server()
    try:
        monkeypatch.setattr(client_mod, "REFRESH_DEADLINE_SECONDS", 0.5)
        desc = make_oauth_descriptor()
        resolved = authorization._declared(desc)
        resolved = authorization.ResolvedAuthorization(
            **{**resolved.__dict__, "token_endpoint": f"http://127.0.0.1:{server.server_address[1]}/token"}
        )
        started = time.monotonic()
        with pytest.raises(TokenRefreshFailedError) as info:
            await client.refresh(descriptor=desc, resolved=resolved, client=ENV_CLIENT, refresh_token="rt")
        assert info.value.reason == "transient"
        assert time.monotonic() - started < 2.0
    finally:
        await shutdown_all_clients()
        server.shutdown()
        server.server_close()


async def test_revoke_of_a_discovered_provider_carries_client_authentication(fake: FakeOAuthServer, allow_loopback):
    fake.route("POST", "/revoke", Reply(status=200))
    resolved = _guarded(revocation_endpoint=f"{fake.base}/revoke")
    outcome = await client.revoke(
        descriptor=make_discovered_descriptor(), resolved=resolved, client=_client("client_secret_basic"), token="tok"
    )
    assert outcome.outcome == "success"
    (request,) = fake.requests_to("/revoke")
    assert request.form == {"token": "tok", "client_id": "c id"}
    assert request.headers["authorization"].startswith("Basic ")


async def test_a_declared_revoke_sends_only_the_token(install_http):
    fake_http = install_http([FakeHttpResponse(status_code=200)])
    desc = make_oauth_descriptor()
    outcome = await client.revoke(descriptor=desc, resolved=authorization._declared(desc), client=None, token="t")
    assert outcome.outcome == "success"
    assert fake_http.posts[0][1] == {"token": "t"}
    assert fake_http.headers[0] == {}


async def test_a_guarded_revoke_without_a_client_is_a_caller_bug():
    with pytest.raises(RuntimeError, match="needs its client"):
        await client.revoke(
            descriptor=make_discovered_descriptor(),
            resolved=_guarded(revocation_endpoint="https://as.example.com/revoke"),
            client=None,
            token="t",
        )


async def test_a_token_endpoint_the_guard_refuses_at_exchange_raises_oauth_error():
    with pytest.raises(CodeExchangeFailedError, match="refused"):
        await client.exchange_code(
            descriptor=make_discovered_descriptor(),
            resolved=_guarded(token_endpoint="https://10.20.30.40/token"),
            client=_client("client_secret_post"),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/cb",
        )


async def test_a_token_endpoint_the_guard_refuses_at_refresh_is_transient():
    with pytest.raises(TokenRefreshFailedError) as info:
        await client.refresh(
            descriptor=make_discovered_descriptor(),
            resolved=_guarded(token_endpoint="https://10.20.30.40/token"),
            client=_client("client_secret_post"),
            refresh_token="rt",
        )
    assert info.value.reason == "transient"


async def test_a_revocation_endpoint_the_guard_refuses_is_a_failed_outcome():
    outcome = await client.revoke(
        descriptor=make_discovered_descriptor(),
        resolved=_guarded(revocation_endpoint="https://169.254.1.1/revoke"),
        client=_client("none", secret=None),
        token="t",
    )
    assert outcome.outcome == "failed"


async def test_a_guarded_exchange_reaches_the_token_endpoint_through_the_guard(fake: FakeOAuthServer, allow_loopback):
    fake.route("POST", "/token", Reply(status=200, body={"access_token": "at", "token_type": "user"}))
    resp = await client.exchange_code(
        descriptor=make_discovered_descriptor(),
        resolved=_guarded(token_endpoint=f"{fake.base}/token"),
        client=_client("none", secret=None),
        code="c",
        code_verifier="v",
        redirect_uri="https://app.example.com/cb",
        resource="https://mcp.example.com/mcp",
        require_refresh_token=False,
    )
    assert resp.access_token == "at"
    assert resp.expires_at is None
    (request,) = fake.requests_to("/token")
    assert request.form["resource"] == "https://mcp.example.com/mcp"
    assert request.form["client_id"] == "c id"
    assert "client_secret" not in request.form


async def test_a_declared_providers_calls_do_not_pass_through_the_guard(install_http, monkeypatch, oauth_client_env):
    async def _guarded(*args, **kwargs):
        raise AssertionError("a declared provider's request must use the pooled client")

    monkeypatch.setattr(client_mod, "guarded_request", _guarded)
    install_http(
        [
            FakeHttpResponse(json_body={"access_token": "at", "refresh_token": "rt", "expires_in": 60}),
            FakeHttpResponse(json_body={"access_token": "at2", "expires_in": 60}),
            FakeHttpResponse(status_code=200),
        ]
    )
    await client.exchange_code(
        **declared_call(), code="c", code_verifier="v", redirect_uri="https://app.example.com/cb"
    )
    await client.refresh(**declared_call(), refresh_token="rt")
    assert (await client.revoke(**declared_call(), token="t")).outcome == "success"
