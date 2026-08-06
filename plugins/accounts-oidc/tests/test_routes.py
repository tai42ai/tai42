"""Tests for the login-flow routes: authorize, callback, SSO exchange, boot guard."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import RSAKey

from tai42_accounts_oidc import oauth, routes
from tai42_accounts_oidc import provider as provider_mod

from .conftest import build_request, response_json

_STATE_KEY = b"hmac-signing-secret"


def _corp(oidc_server: Any, **over: Any) -> dict[str, Any]:
    base = {"name": "corp", "issuer": oidc_server.base_url, "client_id": "client-1", "client_secret": "secret"}
    base.update(over)
    return base


def _location_query(response: Any) -> dict[str, list[str]]:
    return parse_qs(urlsplit(response.headers["location"]).query)


async def _authorize(provider_name: str) -> Any:
    request = build_request(method="GET", path_params={"provider": provider_name}, headers={"host": "evil.example"})
    return await routes.oidc_authorize(request)


def _state_record(fake: Any, state_value: str) -> tuple[str, dict[str, Any]]:
    state_id = oauth.verify_state(state_value, _STATE_KEY)
    assert state_id is not None
    return state_value, json.loads(fake.store[f"acc:oidc:state:{state_id}"])


def _flow_cookie(authorize_response: Any) -> str:
    """The flow-binding cookie value the authorize step set on its 302."""
    header = authorize_response.headers["set-cookie"]
    assert header.startswith(f"{routes._FLOW_COOKIE}=")
    return header.split("=", 1)[1].split(";", 1)[0]


async def _callback(provider_name: str, *, code: str, state: str, binding: str | None = None) -> Any:
    headers = {"cookie": f"{routes._FLOW_COOKIE}={binding}"} if binding is not None else None
    request = build_request(
        method="GET",
        path_params={"provider": provider_name},
        query_string=urlencode({"code": code, "state": state}).encode(),
        headers=headers,
    )
    return await routes.oidc_callback(request)


# -- authorize ------------------------------------------------------------------


async def test_authorize_builds_issuer_url(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    _, fake = make_provider([_corp(oidc_server)])

    response = await _authorize("corp")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{oidc_server.base_url}/authorize?")
    query = _location_query(response)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"]
    assert query["nonce"]
    # The redirect_uri is derived from public_base_url config, NOT the spoofed Host.
    assert query["redirect_uri"] == ["https://studio.example.com/api/login/oidc/corp/callback"]
    assert "evil.example" not in location
    # A single-use state record was stored with the PKCE verifier + nonce.
    _, record = _state_record(fake, query["state"][0])
    assert record["provider"] == "corp"
    assert record["verifier"]
    assert record["nonce"] == query["nonce"][0]


async def test_authorize_unknown_provider_404(make_provider: Any, oidc_server: Any) -> None:
    _, fake = make_provider([_corp(oidc_server)])
    response = await _authorize("nope")
    assert response.status_code == 404
    assert fake.store == {}


async def test_authorize_sets_binding_cookie(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    _, fake = make_provider([_corp(oidc_server)])
    response = await _authorize("corp")
    cookie = response.headers["set-cookie"]
    # HttpOnly + Secure + SameSite=Lax, scoped to the login-flow path.
    assert cookie.startswith(f"{routes._FLOW_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert f"Path={routes._FLOW_COOKIE_PATH}" in cookie
    # The cookie value is the state record's server-side binding.
    _, record = _state_record(fake, _location_query(response)["state"][0])
    assert _flow_cookie(response) == record["binding"]


async def test_authorize_binding_cookie_secure_on_uppercase_https(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    # An uppercase scheme still validates as https, so the cookie stays Secure.
    _, _fake = make_provider([_corp(oidc_server)], public_base_url="HTTPS://studio.example.com")
    response = await _authorize("corp")
    assert "Secure" in response.headers["set-cookie"]


async def test_authorize_binding_cookie_not_secure_on_loopback_http(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    # A loopback-http origin drops the Secure flag so the cookie round-trips; the
    # binding still holds (HttpOnly + SameSite + value match unaffected).
    _, fake = make_provider([_corp(oidc_server)], public_base_url="http://127.0.0.1:8770")
    response = await _authorize("corp")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{routes._FLOW_COOKIE}=")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    _, record = _state_record(fake, _location_query(response)["state"][0])
    assert _flow_cookie(response) == record["binding"]


async def test_authorize_merges_extra_params(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    _, _fake = make_provider([_corp(oidc_server, extra_authorize_params={"organization": "org_1"})])
    response = await _authorize("corp")
    query = _location_query(response)
    # The operator's extra param rides the redirect alongside the flow-owned params.
    assert query["organization"] == ["org_1"]
    assert query["response_type"] == ["code"]
    assert query["state"]


# -- full flow ------------------------------------------------------------------


async def test_full_login_flow(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    instance, fake = make_provider([_corp(oidc_server)])

    authorize = await _authorize("corp")
    binding = _flow_cookie(authorize)
    state_value, record = _state_record(fake, _location_query(authorize)["state"][0])
    # The authorize step stored the binding server-side and mirrored it into the cookie.
    assert record["binding"] == binding
    # The single-use state carries the short round-trip TTL.
    state_key = f"acc:oidc:state:{oauth.verify_state(state_value, _STATE_KEY)}"
    assert fake.expirations[state_key] == provider_mod.STATE_TTL_SECONDS
    oidc_server.set_json(
        "/token",
        {"id_token": oidc_server.id_token(sub="alice", aud="client-1", nonce=record["nonce"]), "access_token": "x"},
    )

    callback = await _callback("corp", code="auth-code", state=state_value, binding=binding)
    assert callback.status_code == 302
    sso_code = _location_query(callback)["sso"][0]
    assert callback.headers["location"] == f"https://studio.example.com/login?sso={sso_code}"
    # The one-time SSO hand-back code carries the short window; the flow cookie is
    # cleared (expired, at the same path so the browser actually drops it).
    assert fake.expirations[f"acc:oidc:sso:{sso_code}"] == provider_mod.SSO_TTL_SECONDS
    assert f"{routes._FLOW_COOKIE}=" in callback.headers["set-cookie"]
    assert "Max-Age=0" in callback.headers["set-cookie"]
    assert f"Path={routes._FLOW_COOKIE_PATH}" in callback.headers["set-cookie"]

    exchange = await routes.sso_exchange(build_request(body={"code": sso_code}, method="POST"))
    data = response_json(exchange)["data"]
    assert data["user_id"] == "oidc:corp:alice"
    token = data["token"]

    # The minted session carries the idle TTL from settings.
    from tai42_accounts_oidc.settings import accounts_oidc_settings

    assert fake.expirations[provider_mod._session_key(token)] == accounts_oidc_settings().session_idle_seconds

    # The minted token validates back through the provider's own validate_token.
    identity = await instance.validate_token(token)
    assert identity is not None
    assert identity.user_id == "oidc:corp:alice"
    # No admin/policy service was ever consulted (deny-by-default); the injected
    # admin stayed None and the flow never touched it.
    assert instance.settings.admin is None


# -- callback failure branches --------------------------------------------------


async def test_callback_issuer_error_handback(
    make_provider: Any, oidc_server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    make_provider([_corp(oidc_server)])
    request = build_request(
        method="GET",
        path_params={"provider": "corp"},
        query_string=urlencode({"error": "access_denied", "error_description": "user said no"}).encode(),
    )
    with caplog.at_level(logging.WARNING):
        response = await routes.oidc_callback(request)
    assert response.status_code == 400
    assert "access_denied" in caplog.text


async def test_callback_unknown_provider_404(make_provider: Any, oidc_server: Any) -> None:
    make_provider([_corp(oidc_server)])
    response = await _callback("nope", code="c", state="s")
    assert response.status_code == 404


async def test_callback_missing_code_or_state(make_provider: Any, oidc_server: Any) -> None:
    make_provider([_corp(oidc_server)])
    request = build_request(method="GET", path_params={"provider": "corp"}, query_string=b"code=only")
    response = await routes.oidc_callback(request)
    assert response.status_code == 400


async def test_callback_tampered_state(make_provider: Any, oidc_server: Any) -> None:
    make_provider([_corp(oidc_server)])
    response = await _callback("corp", code="c", state="forged.deadbeef")
    assert response.status_code == 400


async def test_callback_replayed_state(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    _, fake = make_provider([_corp(oidc_server)])
    authorize = await _authorize("corp")
    binding = _flow_cookie(authorize)
    state_value, record = _state_record(fake, _location_query(authorize)["state"][0])
    oidc_server.set_json(
        "/token",
        {"id_token": oidc_server.id_token(nonce=record["nonce"]), "access_token": "x"},
    )
    # First redemption consumes the single-use state (GETDEL).
    first = await _callback("corp", code="c", state=state_value, binding=binding)
    assert first.status_code == 302
    # Replay: the state record is gone.
    replay = await _callback("corp", code="c", state=state_value, binding=binding)
    assert replay.status_code == 400


async def test_callback_cross_provider_state_confusion(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    _, fake = make_provider(
        [
            _corp(oidc_server),
            _corp(oidc_server, name="other"),
        ]
    )
    authorize = await _authorize("corp")
    binding = _flow_cookie(authorize)
    state_value, _ = _state_record(fake, _location_query(authorize)["state"][0])
    # A state minted for 'corp' must not be redeemable at 'other' (even with the
    # matching flow cookie): the cross-provider guard rejects before binding.
    response = await _callback("other", code="c", state=state_value, binding=binding)
    assert response.status_code == 400


async def _prepare_callback(make_provider: Any, oidc_server: Any, **cfg: Any) -> tuple[str, dict[str, Any], Any]:
    oidc_server.configure_oidc()
    _instance, fake = make_provider([_corp(oidc_server, **cfg)])
    authorize = await _authorize("corp")
    state_value, record = _state_record(fake, _location_query(authorize)["state"][0])
    return state_value, record, oidc_server


async def test_callback_missing_binding_cookie_rejected(
    make_provider: Any, oidc_server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    state_value, _record, server = await _prepare_callback(make_provider, oidc_server)
    # A genuine state redeemed without the flow cookie (login-CSRF) is refused
    # before any token exchange.
    with caplog.at_level(logging.WARNING):
        response = await _callback("corp", code="c", state=state_value)
    assert response.status_code == 400
    assert "browser-binding" in caplog.text
    assert ("POST", "/token") not in server.requests


async def test_callback_wrong_binding_cookie_rejected(make_provider: Any, oidc_server: Any) -> None:
    state_value, _record, server = await _prepare_callback(make_provider, oidc_server)
    # A flow cookie that does not match the state's binding is refused before any
    # token exchange, and the stale flow cookie is cleared at its own path.
    response = await _callback("corp", code="c", state=state_value, binding="not-the-binding")
    assert response.status_code == 400
    assert ("POST", "/token") not in server.requests
    clear = response.headers["set-cookie"]
    assert "Max-Age=0" in clear
    assert f"Path={routes._FLOW_COOKIE_PATH}" in clear


async def test_callback_bad_signature(make_provider: Any, oidc_server: Any, signing_key: RSAKey) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    # Sign with a DIFFERENT key but the served kid — signature verification fails.
    kid = signing_key.kid
    assert kid
    other = RSAKey.generate_key(2048, parameters={"kid": kid})
    forged = joserfc_jwt.encode(
        {"alg": "RS256", "kid": kid},
        {"iss": server.base_url, "aud": "client-1", "sub": "a", "exp": 9999999999, "nonce": record["nonce"]},
        other,
    )
    server.set_json("/token", {"id_token": forged, "access_token": "x"})
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 401


async def test_callback_wrong_aud(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    server.set_json(
        "/token", {"id_token": server.id_token(aud="someone-else", nonce=record["nonce"]), "access_token": "x"}
    )
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 401


async def test_callback_wrong_iss(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    server.set_json(
        "/token",
        {"id_token": server.id_token(nonce=record["nonce"], iss="https://evil.example"), "access_token": "x"},
    )
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 401


async def test_callback_missing_nonce(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    # id_token minted with no nonce while the flow expects one -> replay binding fails.
    server.set_json("/token", {"id_token": server.id_token(), "access_token": "x"})
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 401


async def test_callback_wrong_nonce(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    server.set_json("/token", {"id_token": server.id_token(nonce="not-the-one"), "access_token": "x"})
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 401


async def test_callback_missing_claim(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server, claim="email")
    # The id_token verifies but carries no 'email' claim -> loud 502 misconfiguration.
    server.set_json("/token", {"id_token": server.id_token(nonce=record["nonce"]), "access_token": "x"})
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 502


async def test_callback_token_endpoint_non_200(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    server.set_json("/token", {"error": "bad"}, status=400)
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 502


async def test_callback_token_response_missing_id_token(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    server.set_json("/token", {"access_token": "x"})
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 502


async def test_callback_token_response_non_object(make_provider: Any, oidc_server: Any) -> None:
    state_value, record, server = await _prepare_callback(make_provider, oidc_server)
    server.set_json("/token", [1, 2, 3])  # a JSON array, not an object
    response = await _callback("corp", code="c", state=state_value, binding=record["binding"])
    assert response.status_code == 502


async def test_callback_token_endpoint_transport_error(make_provider: Any, oidc_server: Any) -> None:
    # Discovery advertises a token endpoint on a closed port -> transport failure.
    oidc_server.set_json(
        "/.well-known/openid-configuration",
        {
            "issuer": oidc_server.base_url,
            "jwks_uri": f"{oidc_server.base_url}/jwks",
            "authorization_endpoint": f"{oidc_server.base_url}/authorize",
            "token_endpoint": "http://127.0.0.1:1/token",
        },
    )
    oidc_server.set_json("/jwks", {"keys": [oidc_server.key.as_dict(private=False)]})
    _, fake = make_provider([_corp(oidc_server)])
    authorize = await _authorize("corp")
    binding = _flow_cookie(authorize)
    state_value, _ = _state_record(fake, _location_query(authorize)["state"][0])
    response = await _callback("corp", code="c", state=state_value, binding=binding)
    assert response.status_code == 502


# -- SSO exchange ---------------------------------------------------------------


async def test_sso_exchange_single_use(make_provider: Any) -> None:
    make_provider([{"name": "google", "preset": "google", "client_id": "c", "client_secret": "s"}])
    await provider_mod.store_sso_code("code-1", "tai-sess-tok", "oidc:google:bob")

    first = await routes.sso_exchange(build_request(body={"code": "code-1"}, method="POST"))
    assert response_json(first)["data"] == {"token": "tai-sess-tok", "user_id": "oidc:google:bob"}
    # Second exchange of the same code misses (single-use GETDEL).
    second = await routes.sso_exchange(build_request(body={"code": "code-1"}, method="POST"))
    assert second.status_code == 400


async def test_sso_exchange_unknown_code(make_provider: Any, caplog: pytest.LogCaptureFixture) -> None:
    make_provider([{"name": "google", "preset": "google", "client_id": "c", "client_secret": "s"}])
    with caplog.at_level(logging.WARNING):
        response = await routes.sso_exchange(build_request(body={"code": "nope"}, method="POST"))
    assert response.status_code == 400
    assert "SSO exchange miss" in caplog.text


async def test_sso_exchange_invalid_body(make_provider: Any) -> None:
    make_provider([{"name": "google", "preset": "google", "client_id": "c", "client_secret": "s"}])
    response = await routes.sso_exchange(build_request(body={"not_code": "x"}, method="POST"))
    assert response.status_code == 400


# -- GitHub path ----------------------------------------------------------------


async def test_github_login_flow(make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "GITHUB_AUTHORIZE_URL", f"{oidc_server.base_url}/ghauth")
    monkeypatch.setattr(routes, "GITHUB_TOKEN_URL", f"{oidc_server.base_url}/ghtoken")
    monkeypatch.setattr(routes, "GITHUB_USERINFO_URL", f"{oidc_server.base_url}/ghuser")
    _, fake = make_provider([{"name": "github", "preset": "github", "client_id": "gh", "client_secret": "s"}])

    authorize = await _authorize("github")
    assert authorize.headers["location"].startswith(f"{oidc_server.base_url}/ghauth?")
    query = _location_query(authorize)
    assert "nonce" not in query  # plain OAuth2: no id_token to bind
    binding = _flow_cookie(authorize)
    state_value, _ = _state_record(fake, query["state"][0])

    oidc_server.set_json("/ghtoken", {"access_token": "gh-access"})
    oidc_server.set_json("/ghuser", {"id": 4242, "login": "octocat"})

    callback = await _callback("github", code="c", state=state_value, binding=binding)
    assert callback.status_code == 302
    sso_code = _location_query(callback)["sso"][0]
    exchange = await routes.sso_exchange(build_request(body={"code": sso_code}, method="POST"))
    assert response_json(exchange)["data"]["user_id"] == "oidc:github:4242"


async def _github_provider(
    oidc_server: Any, make_provider: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, str, str]:
    monkeypatch.setattr(routes, "GITHUB_AUTHORIZE_URL", f"{oidc_server.base_url}/ghauth")
    monkeypatch.setattr(routes, "GITHUB_TOKEN_URL", f"{oidc_server.base_url}/ghtoken")
    monkeypatch.setattr(routes, "GITHUB_USERINFO_URL", f"{oidc_server.base_url}/ghuser")
    _, fake = make_provider([{"name": "github", "preset": "github", "client_id": "gh", "client_secret": "s"}])
    authorize = await _authorize("github")
    state_value, _ = _state_record(fake, _location_query(authorize)["state"][0])
    return oidc_server, state_value, _flow_cookie(authorize)


async def test_github_missing_access_token(
    make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state_value, binding = await _github_provider(oidc_server, make_provider, monkeypatch)
    server.set_json("/ghtoken", {"scope": "read:user"})  # no access_token
    response = await _callback("github", code="c", state=state_value, binding=binding)
    assert response.status_code == 502


async def test_github_userinfo_non_object(
    make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state_value, binding = await _github_provider(oidc_server, make_provider, monkeypatch)
    server.set_json("/ghtoken", {"access_token": "gh-access"})
    server.set_json("/ghuser", ["not", "an", "object"])
    response = await _callback("github", code="c", state=state_value, binding=binding)
    assert response.status_code == 502


async def test_github_userinfo_transport_error(
    make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state_value, binding = await _github_provider(oidc_server, make_provider, monkeypatch)
    server.set_json("/ghtoken", {"access_token": "gh-access"})
    monkeypatch.setattr(routes, "GITHUB_USERINFO_URL", "http://127.0.0.1:1/user")  # closed port
    response = await _callback("github", code="c", state=state_value, binding=binding)
    assert response.status_code == 502


async def test_github_userinfo_non_200(make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "GITHUB_AUTHORIZE_URL", f"{oidc_server.base_url}/ghauth")
    monkeypatch.setattr(routes, "GITHUB_TOKEN_URL", f"{oidc_server.base_url}/ghtoken")
    monkeypatch.setattr(routes, "GITHUB_USERINFO_URL", f"{oidc_server.base_url}/ghuser")
    _, fake = make_provider([{"name": "github", "preset": "github", "client_id": "gh", "client_secret": "s"}])
    authorize = await _authorize("github")
    binding = _flow_cookie(authorize)
    state_value, _ = _state_record(fake, _location_query(authorize)["state"][0])
    oidc_server.set_json("/ghtoken", {"access_token": "gh-access"})
    oidc_server.set_json("/ghuser", {"error": "nope"}, status=401)
    response = await _callback("github", code="c", state=state_value, binding=binding)
    assert response.status_code == 502


# -- boot guard -----------------------------------------------------------------


def test_boot_guard_raises_when_holder_unset() -> None:
    provider_mod.reset_provider_settings()
    with pytest.raises(RuntimeError, match="never instantiated"):
        routes._assert_accounts_provider_instantiated()


def test_boot_guard_passes_when_holder_set(make_provider: Any) -> None:
    make_provider([{"name": "google", "preset": "google", "client_id": "c", "client_secret": "s"}])
    routes._assert_accounts_provider_instantiated()  # no raise
