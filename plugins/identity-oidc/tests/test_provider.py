"""Tests for the validate-only OIDC identity provider.

Driven against a real loopback OIDC issuer (the ``oidc_server`` fixture) with
in-test RSA keys minting real signed JWTs, so the suite runs with zero real
network I/O.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import ApiKeyIdentityProvider, AuthIdentity, IdentityProvider
from tai42_contract.access_control.registry import get_identity_provider_factory
from tai42_kit.net.jwt import JwksFetchError, JwtVerifyError

from tai42_identity_oidc.provider import OidcIdentityProvider

from .conftest import AcSettingsStub, OidcServer, make_rsa_key, sign


def _claims(server: OidcServer, **overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": server.base_url,
        "aud": "client-1",
        "sub": "subject-1",
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return claims


@pytest.fixture
def provider(oidc_server: OidcServer, monkeypatch: pytest.MonkeyPatch) -> OidcIdentityProvider:
    """A provider wired to the loopback issuer (discovery + default key already
    served by the fixture)."""
    monkeypatch.setenv("TAI_IDENTITY_OIDC_ISSUER", oidc_server.base_url)
    monkeypatch.setenv("TAI_IDENTITY_OIDC_AUDIENCE", "client-1")
    return OidcIdentityProvider(AcSettingsStub())


# --- happy path -------------------------------------------------------------


async def test_valid_jwt_returns_identity(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    token = oidc_server.sign(_claims(oidc_server, extra="value"))
    identity = await provider.validate_token(token)
    assert isinstance(identity, AuthIdentity)
    assert identity.user_id == f"idp:{oidc_server.base_url}:subject-1"
    # The full verified claims ride into the identity.
    assert identity.claims["sub"] == "subject-1"
    assert identity.claims["extra"] == "value"
    assert identity.claims["iss"] == oidc_server.base_url


async def test_custom_claim_maps_user_id(oidc_server: OidcServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_IDENTITY_OIDC_ISSUER", oidc_server.base_url)
    monkeypatch.setenv("TAI_IDENTITY_OIDC_AUDIENCE", "client-1")
    monkeypatch.setenv("TAI_IDENTITY_OIDC_CLAIM", "email")
    provider = OidcIdentityProvider(AcSettingsStub())
    token = oidc_server.sign(_claims(oidc_server, email="ada@example.com"))
    identity = await provider.validate_token(token)
    assert identity is not None
    assert identity.user_id == f"idp:{oidc_server.base_url}:ada@example.com"


# --- structural gate --------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["sk-abc123", "tai-sess-abc123", "garbage", "aaa.bbb", "aaa.bbb.ccc.ddd", ""],
)
async def test_non_jwt_returns_none_without_fetch(
    provider: OidcIdentityProvider, oidc_server: OidcServer, token: str
) -> None:
    assert await provider.validate_token(token) is None
    # The structural gate must short-circuit before any discovery/JWKS fetch.
    assert oidc_server.requests == []


# --- verification failures raise (fail-closed) ------------------------------


async def test_bad_signature_raises(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    # A different private key under the kid the JWKS advertises.
    impostor = make_rsa_key(oidc_server.default_kid)
    token = sign(impostor, _claims(oidc_server), kid=oidc_server.default_kid)
    with pytest.raises(JwtVerifyError, match="signature verification failed"):
        await provider.validate_token(token)


async def test_wrong_issuer_raises(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    token = oidc_server.sign(_claims(oidc_server, iss="https://evil.test"))
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await provider.validate_token(token)


async def test_wrong_audience_raises(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    token = oidc_server.sign(_claims(oidc_server, aud="someone-else"))
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await provider.validate_token(token)


async def test_expired_raises(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    token = oidc_server.sign(_claims(oidc_server, exp=int(time.time()) - 300))
    with pytest.raises(JwtVerifyError, match="claims verification failed"):
        await provider.validate_token(token)


async def test_unknown_kid_refetch_once(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    # Populate the cache with the default key.
    await provider.validate_token(oidc_server.sign(_claims(oidc_server)))
    assert oidc_server.requests.count("/jwks") == 1

    # The issuer rotates in a second key; the unknown kid forces exactly one JWKS
    # refetch, after which the new-key token verifies.
    oidc_server.add_key("sig-key-2")
    oidc_server.publish(oidc_server.default_kid, "sig-key-2")
    token = oidc_server.sign(_claims(oidc_server, sub="subject-2"), kid="sig-key-2")
    identity = await provider.validate_token(token)
    assert identity is not None
    assert identity.user_id == f"idp:{oidc_server.base_url}:subject-2"
    assert oidc_server.requests.count("/jwks") == 2


# --- claim / config edges ---------------------------------------------------


async def test_missing_configured_claim_raises(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    claims = _claims(oidc_server)
    del claims["sub"]  # verifies fine (sub is not a registered claim) but has no subject
    token = oidc_server.sign(claims)
    with pytest.raises(ValueError, match="has no 'sub' claim"):
        await provider.validate_token(token)


async def test_owner_user_id_claim_stripped(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    # An issuer that emits the reserved owner claim must not have it reach the
    # identity — the plugin strips it as defense-in-depth.
    token = oidc_server.sign(_claims(oidc_server, **{OWNER_USER_ID_CLAIM: "usr-admin"}))
    identity = await provider.validate_token(token)
    assert identity is not None
    assert OWNER_USER_ID_CLAIM not in identity.claims
    assert identity.user_id == f"idp:{oidc_server.base_url}:subject-1"


async def test_missing_required_settings_raises() -> None:
    # Env is cleared by the autouse fixture, so issuer/audience are unset — the
    # loud error names the missing env var.
    with pytest.raises(ValueError, match="TAI_IDENTITY_OIDC_ISSUER is required"):
        OidcIdentityProvider(AcSettingsStub())


async def test_empty_allowed_algs_rejected(oidc_server: OidcServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_IDENTITY_OIDC_ISSUER", oidc_server.base_url)
    monkeypatch.setenv("TAI_IDENTITY_OIDC_AUDIENCE", "client-1")
    monkeypatch.setenv("TAI_IDENTITY_OIDC_ALLOWED_ALGS", "[]")
    with pytest.raises(ValidationError):
        OidcIdentityProvider(AcSettingsStub())


# --- healthcheck ------------------------------------------------------------


async def test_healthcheck_reachable_passes(provider: OidcIdentityProvider, oidc_server: OidcServer) -> None:
    # Discovery + JWKS both reachable and well-formed: the probe completes without
    # raising (the sentinel kid the live JWKS lacks is not a health failure).
    await provider.healthcheck()
    assert oidc_server.requests.count("/.well-known/openid-configuration") >= 1
    assert oidc_server.requests.count("/jwks") >= 1


async def test_healthcheck_unreachable_issuer_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A closed loopback port: discovery cannot be fetched, so the boot probe fails
    # loudly rather than serving a broken issuer.
    monkeypatch.setenv("TAI_IDENTITY_OIDC_ISSUER", "http://127.0.0.1:1")
    monkeypatch.setenv("TAI_IDENTITY_OIDC_AUDIENCE", "client-1")
    provider = OidcIdentityProvider(AcSettingsStub())
    with pytest.raises(JwksFetchError):
        await provider.healthcheck()


# --- capability gate + registration -----------------------------------------


def test_provider_is_not_mintable(provider: OidcIdentityProvider) -> None:
    # Validate-only: an IdentityProvider but not an ApiKeyIdentityProvider, so the
    # key-minting surface refuses it.
    assert isinstance(provider, IdentityProvider)
    assert not isinstance(provider, ApiKeyIdentityProvider)


def test_registration_landed() -> None:
    assert get_identity_provider_factory("identity-oidc") is OidcIdentityProvider
