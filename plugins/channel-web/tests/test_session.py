"""The visitor session — token and visitor-id minting, the cookie-shape read (a
non-minted value is no session), the Secure-conditional cookie name and path, the
cookie attributes, the cross-origin guard, and the navigation check that fences the
mint path."""

from __future__ import annotations

import pytest
from starlette.responses import JSONResponse

from tai42_channel_web.session import (
    is_cross_origin,
    is_document_navigation,
    mint_session_token,
    mint_visitor_id,
    session_cookie_name,
    session_cookie_path,
    session_token,
    set_session_cookie,
)
from tai42_channel_web.settings import WebSettings
from tests.conftest import ORIGIN, PLAIN_COOKIE, SECURE_COOKIE, SESSION_TOKEN, build_request


def test_minted_tokens_are_unguessable_and_unique():
    tokens = {mint_session_token() for _ in range(50)}
    assert len(tokens) == 50
    # >=128 bits of entropy, urlsafe-encoded.
    assert all(len(value) >= 22 for value in tokens)


def test_minted_visitor_ids_are_unique_and_colon_free():
    ids = {mint_visitor_id() for _ in range(50)}
    assert len(ids) == 50
    # The address qualifies a transcript key and closes a composite recipient.
    assert all(":" not in value for value in ids)


def test_a_visitor_id_is_never_the_token():
    # The address is published (bridge threads, operator plane); the token is the
    # bearer secret. Minting them separately is what keeps reading one harmless.
    assert mint_visitor_id() != mint_session_token()


def _plain(monkeypatch: pytest.MonkeyPatch) -> WebSettings:
    """A plain-http deployment's settings — the mode a local or e2e stack runs in."""
    monkeypatch.setenv("CHANNEL_WEB_SESSION_COOKIE_SECURE", "false")
    return WebSettings()


def test_session_token_reads_the_cookie(no_web_env):
    assert session_token(build_request(token=SESSION_TOKEN), WebSettings()) == SESSION_TOKEN


def test_session_token_accepts_a_freshly_minted_value(no_web_env):
    minted = mint_session_token()
    assert session_token(build_request(token=minted), WebSettings()) == minted


def test_session_token_without_a_cookie_is_none(no_web_env):
    assert session_token(build_request(), WebSettings()) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        # A separator would reach a Redis key as a second field.
        "aaaaaaaaaaaaaaaaaaaaaa:bbbb",
        "a" * 65,
        "aaaaaaaaaaaaaaaaaaaaaa!",
    ],
)
def test_session_token_rejects_a_non_minted_value(no_web_env, value: str):
    assert session_token(build_request(cookie=f"{SECURE_COOKIE}={value}"), WebSettings()) is None


def test_the_cookie_name_and_path_follow_the_secure_flag():
    # ``__Host-`` is honored by a browser only on a Secure, Domain-less cookie at the
    # root path, and then no sibling host of this origin can plant one. All three go
    # together or none does.
    assert session_cookie_name(True) == "__Host-tai_web_session"
    assert session_cookie_path(True) == "/"
    assert session_cookie_name(False) == "tai_web_session"
    assert session_cookie_path(False) == "/api/channels/web"


def test_a_secure_deployment_reads_only_the_host_prefixed_cookie(no_web_env):
    # Only ONE name is this deployment's: an unprefixed cookie is another
    # deployment's (or an attacker's) and is not read back as a session.
    settings = WebSettings()
    assert session_token(build_request(token=SESSION_TOKEN, cookie_name=PLAIN_COOKIE), settings) is None
    assert session_token(build_request(token=SESSION_TOKEN, cookie_name=SECURE_COOKIE), settings) == SESSION_TOKEN


def test_a_plain_http_deployment_reads_only_the_unprefixed_cookie(no_web_env, monkeypatch: pytest.MonkeyPatch):
    # A browser would never have stored a ``__Host-`` cookie over plain http, so the
    # prefixed name there can only be a hand-set one.
    settings = _plain(monkeypatch)
    assert session_token(build_request(token=SESSION_TOKEN, cookie_name=SECURE_COOKIE), settings) is None
    assert session_token(build_request(token=SESSION_TOKEN, cookie_name=PLAIN_COOKIE), settings) == SESSION_TOKEN


def test_set_session_cookie_carries_the_full_attribute_set(no_web_env):
    response = JSONResponse({})
    set_session_cookie(response, SESSION_TOKEN, WebSettings())
    header = response.headers["set-cookie"]
    # The three attributes a browser demands before it accepts the ``__Host-`` prefix:
    # Secure, Path=/, and no Domain.
    assert header.startswith(f"{SECURE_COOKIE}={SESSION_TOKEN}")
    assert "Path=/;" in header
    assert "Domain" not in header
    assert f"Max-Age={30 * 86400}" in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header


def test_set_session_cookie_drops_secure_and_the_prefix_for_a_plain_http_deployment(
    no_web_env, monkeypatch: pytest.MonkeyPatch
):
    # A ``__Host-`` cookie is REFUSED by the browser without Secure, so a plain-http
    # stack would hand every visitor a session it can never store.
    response = JSONResponse({})
    set_session_cookie(response, SESSION_TOKEN, _plain(monkeypatch))
    header = response.headers["set-cookie"]
    assert header.startswith(f"{PLAIN_COOKIE}={SESSION_TOKEN}")
    assert "Secure" not in header
    assert "Path=/api/channels/web" in header


def test_no_origin_header_is_not_cross_origin():
    assert is_cross_origin(build_request()) is False


def test_matching_origin_is_not_cross_origin():
    assert is_cross_origin(build_request(extra_headers=[(b"origin", ORIGIN.encode())])) is False


def test_trailing_slash_origin_still_matches():
    assert is_cross_origin(build_request(extra_headers=[(b"origin", f"{ORIGIN}/".encode())])) is False


def test_foreign_origin_is_cross_origin():
    assert is_cross_origin(build_request(extra_headers=[(b"origin", b"https://evil.example")])) is True


def test_origin_is_compared_against_the_forwarded_public_origin():
    # Behind a TLS proxy the request arrives on another scheme/host; the browser's
    # Origin names the PUBLIC one, so the forwarded headers are what it must match.
    forwarded = [
        (b"origin", b"https://public.example"),
        (b"x-forwarded-proto", b"https, http"),
        (b"x-forwarded-host", b"public.example"),
    ]
    assert is_cross_origin(build_request(extra_headers=forwarded)) is False


def test_origin_falls_back_to_the_request_url_without_a_host_header():
    request = build_request(extra_headers=[(b"origin", b"https://app-internal:8000")])
    request.scope["headers"] = [(key, value) for key, value in request.scope["headers"] if key != b"host"]
    assert is_cross_origin(request) is False


def test_a_declared_document_load_is_a_navigation():
    assert is_document_navigation(build_request(extra_headers=[(b"sec-fetch-dest", b"Document ")])) is True


def test_a_browser_that_says_nothing_is_admitted():
    # The header is absent on older browsers and on every non-browser client; the
    # guard removes a cross-site denial, it was never a credential.
    assert is_document_navigation(build_request()) is True


@pytest.mark.parametrize("dest", [b"image", b"script", b"empty", b"iframe"])
def test_a_subresource_load_is_not_a_navigation(dest: bytes):
    assert is_document_navigation(build_request(extra_headers=[(b"sec-fetch-dest", dest)])) is False
