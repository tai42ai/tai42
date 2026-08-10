"""Tests for :mod:`tai42_kit.net.jwt` driven against a real loopback OIDC issuer.

RSA keypairs are generated in-test via joserfc and the issuer's discovery + JWKS
are served by the ``oidc_server`` fixture, so the whole suite runs with zero real
network I/O.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import RSAKey

from tai42_kit.net.jwt import (
    JwksCache,
    JwksFetchError,
    JwtVerifyError,
    OidcDiscovery,
    fetch_discovery,
    looks_like_jwt,
    verify_jwt,
)


def _make_key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid})


def _jwks(*keys: RSAKey) -> dict[str, Any]:
    return {"keys": [key.as_dict(private=False) for key in keys]}


def _sign(key: RSAKey, claims: dict[str, Any], *, kid: str, alg: str = "RS256") -> str:
    return joserfc_jwt.encode({"alg": alg, "kid": kid}, claims, key)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _valid_claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": "https://issuer.test",
        "aud": "client-1",
        "sub": "subject-1",
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return claims


# --- looks_like_jwt ---------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("aaa.bbb.ccc", True),
        ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln", True),
        ("sk-abc123", False),
        ("tai-sess-abc123", False),
        ("aaa.bbb", False),
        ("aaa.bbb.ccc.ddd", False),
        ("aaa..ccc", False),
        ("", False),
        ("aaa.bbb.ccc ", False),
    ],
)
def test_looks_like_jwt_truth_table(token: str, expected: bool) -> None:
    assert looks_like_jwt(token) is expected


# --- discovery --------------------------------------------------------------


def _configure_discovery(server: Any, *, issuer: str | None = None) -> str:
    resolved: str = issuer if issuer is not None else server.base_url
    server.set_json(
        "/.well-known/openid-configuration",
        {
            "issuer": resolved,
            "jwks_uri": f"{server.base_url}/jwks",
            "authorization_endpoint": f"{server.base_url}/authorize",
            "token_endpoint": f"{server.base_url}/token",
            "userinfo_endpoint": f"{server.base_url}/userinfo",
        },
    )
    return resolved


async def test_fetch_discovery_happy(oidc_server: Any) -> None:
    _configure_discovery(oidc_server)
    discovery = await fetch_discovery(oidc_server.base_url)
    assert isinstance(discovery, OidcDiscovery)
    assert discovery.issuer == oidc_server.base_url
    assert discovery.jwks_uri == f"{oidc_server.base_url}/jwks"
    assert discovery.userinfo_endpoint == f"{oidc_server.base_url}/userinfo"


async def test_fetch_discovery_trailing_slash_issuer(oidc_server: Any) -> None:
    # The well-known path is appended to the rstripped issuer; a trailing slash
    # in the configured issuer must not produce a doubled slash that 404s.
    issuer = oidc_server.base_url + "/"
    _configure_discovery(oidc_server, issuer=issuer)
    discovery = await fetch_discovery(issuer)
    assert "//.well-known" not in oidc_server.requests[0]
    assert discovery.jwks_uri == f"{oidc_server.base_url}/jwks"


async def test_fetch_discovery_issuer_mismatch(oidc_server: Any) -> None:
    _configure_discovery(oidc_server, issuer="https://evil.test")
    with pytest.raises(JwksFetchError, match="does not match the configured issuer"):
        await fetch_discovery(oidc_server.base_url)


async def test_fetch_discovery_missing_field(oidc_server: Any) -> None:
    oidc_server.set_json(
        "/.well-known/openid-configuration",
        {"issuer": oidc_server.base_url, "authorization_endpoint": "a", "token_endpoint": "t"},
    )
    with pytest.raises(JwksFetchError, match="jwks_uri"):
        await fetch_discovery(oidc_server.base_url)


async def test_fetch_discovery_redirect(oidc_server: Any) -> None:
    oidc_server.set_route("/.well-known/openid-configuration", location="https://elsewhere.test/")
    with pytest.raises(JwksFetchError, match="redirect"):
        await fetch_discovery(oidc_server.base_url)


async def test_fetch_discovery_size_cap(oidc_server: Any) -> None:
    oidc_server.set_route("/.well-known/openid-configuration", body=b"x" * 500)
    with pytest.raises(JwksFetchError, match="size cap"):
        await fetch_discovery(oidc_server.base_url, max_bytes=100)


async def test_fetch_discovery_bad_status(oidc_server: Any) -> None:
    oidc_server.set_route("/.well-known/openid-configuration", status=500, body=b"boom")
    with pytest.raises(JwksFetchError, match="status 500"):
        await fetch_discovery(oidc_server.base_url)


async def test_fetch_discovery_bad_json(oidc_server: Any) -> None:
    oidc_server.set_route("/.well-known/openid-configuration", body=b"not json")
    with pytest.raises(JwksFetchError, match="not valid JSON"):
        await fetch_discovery(oidc_server.base_url)


async def test_fetch_discovery_http_non_loopback_rejected() -> None:
    with pytest.raises(JwksFetchError, match="https is required"):
        await fetch_discovery("http://issuer.example.com")


async def test_fetch_discovery_non_object_json(oidc_server: Any) -> None:
    oidc_server.set_route("/.well-known/openid-configuration", body=b"[1, 2, 3]")
    with pytest.raises(JwksFetchError, match="not a JSON object"):
        await fetch_discovery(oidc_server.base_url)


async def test_fetch_discovery_without_userinfo(oidc_server: Any) -> None:
    oidc_server.set_json(
        "/.well-known/openid-configuration",
        {
            "issuer": oidc_server.base_url,
            "jwks_uri": f"{oidc_server.base_url}/jwks",
            "authorization_endpoint": f"{oidc_server.base_url}/authorize",
            "token_endpoint": f"{oidc_server.base_url}/token",
        },
    )
    discovery = await fetch_discovery(oidc_server.base_url)
    assert discovery.userinfo_endpoint is None


async def test_fetch_discovery_transport_error() -> None:
    # A closed loopback port: the connection is refused, surfacing as a typed
    # transport error rather than a raw httpx exception.
    with pytest.raises(JwksFetchError, match="Transport error"):
        await fetch_discovery("http://127.0.0.1:1", timeout=1.0)


# --- JWKS cache -------------------------------------------------------------


async def test_jwks_cache_hit_fetches_once(oidc_server: Any) -> None:
    key = _make_key("k1")
    oidc_server.set_json("/jwks", _jwks(key))
    cache = JwksCache(f"{oidc_server.base_url}/jwks")
    first = await cache.get_key("k1", "RS256")
    second = await cache.get_key("k1", "RS256")
    assert first is second
    assert oidc_server.requests.count("/jwks") == 1


async def test_jwks_cache_unknown_kid_refetches_and_rotates(oidc_server: Any) -> None:
    k1 = _make_key("k1")
    oidc_server.set_json("/jwks", _jwks(k1))
    cache = JwksCache(f"{oidc_server.base_url}/jwks")
    await cache.get_key("k1", "RS256")
    assert oidc_server.requests.count("/jwks") == 1

    # Issuer rotates in k2; the unknown kid forces exactly one refetch.
    k2 = _make_key("k2")
    oidc_server.set_json("/jwks", _jwks(k1, k2))
    rotated = await cache.get_key("k2", "RS256")
    assert rotated.kid == "k2"
    assert oidc_server.requests.count("/jwks") == 2


async def test_jwks_cache_unknown_kid_within_cooldown_no_refetch(oidc_server: Any) -> None:
    k1 = _make_key("k1")
    oidc_server.set_json("/jwks", _jwks(k1))
    cache = JwksCache(f"{oidc_server.base_url}/jwks")

    # First unknown kid: initial populate (1) + one forced refetch (2), still miss.
    with pytest.raises(JwtVerifyError, match="after a JWKS refetch"):
        await cache.get_key("missing-1", "RS256")
    assert oidc_server.requests.count("/jwks") == 2

    # Second unknown kid inside the cooldown: refused with no further fetch.
    with pytest.raises(JwtVerifyError, match="within its"):
        await cache.get_key("missing-2", "RS256")
    assert oidc_server.requests.count("/jwks") == 2


async def test_jwks_cache_ttl_refresh(oidc_server: Any) -> None:
    k1 = _make_key("k1")
    oidc_server.set_json("/jwks", _jwks(k1))
    cache = JwksCache(f"{oidc_server.base_url}/jwks", ttl_seconds=0.0)
    await cache.get_key("k1", "RS256")
    await cache.get_key("k1", "RS256")
    # A zero TTL means every lookup re-reads the JWKS.
    assert oidc_server.requests.count("/jwks") == 2


async def test_jwks_cache_malformed_document(oidc_server: Any) -> None:
    oidc_server.set_json("/jwks", {"keys": [{"kty": "RSA", "kid": "broken"}]})
    cache = JwksCache(f"{oidc_server.base_url}/jwks")
    with pytest.raises(JwksFetchError, match="not a valid key set"):
        await cache.get_key("broken", "RS256")


# --- verify_jwt -------------------------------------------------------------


@pytest.fixture
def issuer_key() -> RSAKey:
    return _make_key("k1")


@pytest.fixture
def jwks_cache(oidc_server: Any, issuer_key: RSAKey) -> JwksCache:
    oidc_server.set_json("/jwks", _jwks(issuer_key))
    return JwksCache(f"{oidc_server.base_url}/jwks")


async def test_verify_jwt_happy(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(), kid="k1")
    claims = await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")
    assert claims["sub"] == "subject-1"
    assert claims["iss"] == "https://issuer.test"


async def test_verify_jwt_aud_list(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(aud=["client-1", "other"]), kid="k1")
    claims = await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")
    assert claims["aud"] == ["client-1", "other"]


async def test_verify_jwt_alg_none_rejected(jwks_cache: JwksCache) -> None:
    header = _b64url(json.dumps({"alg": "none", "kid": "k1"}).encode())
    payload = _b64url(json.dumps(_valid_claims()).encode())
    token = f"{header}.{payload}."
    # An empty third segment is not base64url-shaped, so the structural gate trips
    # first; a token with a bogus signature segment still fails on the alg check.
    forged = f"{header}.{payload}.{_b64url(b'sig')}"
    with pytest.raises(JwtVerifyError, match="not a structurally valid JWT"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")
    with pytest.raises(JwtVerifyError, match="alg 'none' is not in the allowed"):
        await verify_jwt(forged, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_alg_outside_allowlist(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(), kid="k1")
    with pytest.raises(JwtVerifyError, match="not in the allowed"):
        await verify_jwt(
            token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1", allowed_algs=("ES256",)
        )


async def test_verify_jwt_missing_kid(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    header = _b64url(json.dumps({"alg": "RS256"}).encode())
    payload = _b64url(json.dumps(_valid_claims()).encode())
    token = f"{header}.{payload}.{_b64url(b'sig')}"
    with pytest.raises(JwtVerifyError, match="missing a 'kid'"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_malformed_header(jwks_cache: JwksCache) -> None:
    token = f"{_b64url(b'not-json')}.{_b64url(b'x')}.{_b64url(b'sig')}"
    with pytest.raises(JwtVerifyError, match="header is not valid base64url JSON"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_header_not_object(jwks_cache: JwksCache) -> None:
    header = _b64url(json.dumps(["not", "an", "object"]).encode())
    token = f"{header}.{_b64url(b'x')}.{_b64url(b'sig')}"
    with pytest.raises(JwtVerifyError, match="header is not a JSON object"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_bad_signature(jwks_cache: JwksCache) -> None:
    # A different private key under the same kid the JWKS advertises.
    impostor = _make_key("k1")
    token = _sign(impostor, _valid_claims(), kid="k1")
    with pytest.raises(JwtVerifyError, match="signature verification failed"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_wrong_issuer(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(iss="https://evil.test"), kid="k1")
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_wrong_audience(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(aud="someone-else"), kid="k1")
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_expired_beyond_leeway(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(exp=int(time.time()) - 300), kid="k1")
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_expired_within_leeway_passes(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(exp=int(time.time()) - 30), kid="k1")
    claims = await verify_jwt(
        token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1", leeway_seconds=60.0
    )
    assert claims["sub"] == "subject-1"


async def test_verify_jwt_nbf_in_future(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(nbf=int(time.time()) + 3600), kid="k1")
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_missing_exp(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    claims = _valid_claims()
    del claims["exp"]
    token = _sign(issuer_key, claims, kid="k1")
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await verify_jwt(token, jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")


async def test_verify_jwt_unknown_kid_refetch_success(oidc_server: Any) -> None:
    k1 = _make_key("k1")
    oidc_server.set_json("/jwks", _jwks(k1))
    cache = JwksCache(f"{oidc_server.base_url}/jwks")
    await cache.get_key("k1", "RS256")

    k2 = _make_key("k2")
    oidc_server.set_json("/jwks", _jwks(k1, k2))
    token = _sign(k2, _valid_claims(), kid="k2")
    claims = await verify_jwt(token, jwks=cache, issuer="https://issuer.test", audience="client-1")
    assert claims["sub"] == "subject-1"


async def test_verify_jwt_nonce_matching(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(nonce="expected-nonce"), kid="k1")
    claims = await verify_jwt(
        token,
        jwks=jwks_cache,
        issuer="https://issuer.test",
        audience="client-1",
        expected_nonce="expected-nonce",
    )
    assert claims["nonce"] == "expected-nonce"


async def test_verify_jwt_nonce_missing_rejected(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(), kid="k1")
    with pytest.raises(JwtVerifyError, match="nonce"):
        await verify_jwt(
            token,
            jwks=jwks_cache,
            issuer="https://issuer.test",
            audience="client-1",
            expected_nonce="expected-nonce",
        )


async def test_verify_jwt_nonce_mismatch_rejected(jwks_cache: JwksCache, issuer_key: RSAKey) -> None:
    token = _sign(issuer_key, _valid_claims(nonce="other"), kid="k1")
    with pytest.raises(JwtVerifyError, match="nonce"):
        await verify_jwt(
            token,
            jwks=jwks_cache,
            issuer="https://issuer.test",
            audience="client-1",
            expected_nonce="expected-nonce",
        )


async def test_verify_jwt_non_jwt_rejected(jwks_cache: JwksCache) -> None:
    with pytest.raises(JwtVerifyError, match="not a structurally valid JWT"):
        await verify_jwt("sk-abc123", jwks=jwks_cache, issuer="https://issuer.test", audience="client-1")
