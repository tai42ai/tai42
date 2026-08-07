"""Tests for OidcAccountsProvider: sessions, login methods, healthcheck, registration."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.registry import get_identity_provider_factory
from tai42_contract.accounts import get_accounts_provider_factory
from tai42_kit.net.jwt import JwksFetchError

from tai42_accounts_oidc import provider as provider_mod
from tai42_accounts_oidc.provider import OidcAccountsProvider

from .conftest import FakeRedis

_GOOGLE = {"name": "google", "preset": "google", "client_id": "cid", "client_secret": "secret"}


# -- settings holder ------------------------------------------------------------


def test_provider_settings_raises_before_population() -> None:
    with pytest.raises(RuntimeError, match="no active provider"):
        provider_mod.provider_settings()


def test_init_populates_holder(make_provider: Any) -> None:
    instance, _ = make_provider([_GOOGLE])
    assert provider_mod.provider_settings_populated()
    assert provider_mod.provider_settings() is instance.settings


def test_missing_state_key_raises(make_provider: Any) -> None:
    with pytest.raises(ValueError, match="TAI_ACCOUNTS_OIDC_STATE_KEY"):
        make_provider([_GOOGLE], state_key=None)


def test_missing_public_base_url_raises(make_provider: Any) -> None:
    with pytest.raises(ValueError, match="TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL"):
        make_provider([_GOOGLE], public_base_url=None)


# -- sessions -------------------------------------------------------------------


async def test_mint_and_validate_session(make_provider: Any) -> None:
    instance, fake = make_provider([_GOOGLE])
    token = await provider_mod.mint_session("oidc:google:alice")
    assert token.startswith("tai-sess-")

    identity = await instance.validate_token(token)
    assert isinstance(identity, AuthIdentity)
    assert identity.user_id == "oidc:google:alice"
    # Sliding idle refresh happened; the claims carry only plugin-minted fields.
    assert fake.pexpire_calls
    assert fake.pexpire_calls[-1][1] == 86400 * 1000
    assert set(identity.claims) == {"user_id", "created_at", "absolute_deadline"}


async def test_validate_non_session_token_returns_none(make_provider: Any) -> None:
    instance, fake = make_provider([_GOOGLE])
    assert await instance.validate_token("sk-not-ours") is None
    # Fast-rejected without any Redis record touched.
    assert fake.store == {}


async def test_validate_unknown_token_returns_none(make_provider: Any) -> None:
    instance, _ = make_provider([_GOOGLE])
    assert await instance.validate_token("tai-sess-unknown") is None


async def test_validate_absolute_expired_deletes_and_denies(make_provider: Any) -> None:
    instance, fake = make_provider([_GOOGLE])
    token = "tai-sess-expired"
    key = provider_mod._session_key(token)
    now = int(time.time())
    fake.store[key] = json.dumps({"user_id": "u1", "created_at": now - 100, "absolute_deadline": now - 10})

    assert await instance.validate_token(token) is None
    assert key not in fake.store  # deleted


async def test_validate_sliding_does_not_extend_absolute(make_provider: Any) -> None:
    instance, fake = make_provider([_GOOGLE])
    token = await provider_mod.mint_session("u1")
    key = provider_mod._session_key(token)
    absolute_before = json.loads(fake.store[key])["absolute_deadline"]

    identity = await instance.validate_token(token)
    assert identity is not None
    assert identity.claims["absolute_deadline"] == absolute_before


async def test_validate_backend_error_raises(make_provider: Any) -> None:
    fake = FakeRedis(raise_on={"get"})
    instance, _ = make_provider([_GOOGLE], redis=fake)
    with pytest.raises(RuntimeError, match="injected redis failure"):
        await instance.validate_token("tai-sess-boom")


async def test_revoke_session(make_provider: Any) -> None:
    instance, _ = make_provider([_GOOGLE])
    token = await provider_mod.mint_session("u1")
    assert await instance.revoke_session(token) is True
    # Second revoke: record gone.
    assert await instance.revoke_session(token) is False


async def test_revoke_foreign_token_returns_false(make_provider: Any) -> None:
    instance, fake = make_provider([_GOOGLE])
    assert await instance.revoke_session("sk-foreign") is False
    assert fake.store == {}


async def test_needs_bootstrap_is_false(make_provider: Any) -> None:
    instance, _ = make_provider([_GOOGLE])
    assert await instance.needs_bootstrap() is False


# -- login methods --------------------------------------------------------------


def test_login_methods_per_provider(make_provider: Any) -> None:
    instance, _ = make_provider(
        [
            {"name": "google", "preset": "google", "client_id": "c", "client_secret": "s"},
            {
                "name": "corp",
                "issuer": "https://corp.example",
                "client_id": "c",
                "client_secret": "s",
                "display": {"label": "Corp", "icon": "<svg/>"},
            },
        ]
    )
    methods = instance.login_methods()
    assert [m.id for m in methods] == ["google", "corp"]
    assert methods[0].href == "/api/login/oidc/google/authorize"
    assert methods[1].label == "Corp"
    assert methods[1].icon == "<svg/>"


def test_login_methods_empty_when_no_providers(make_provider: Any) -> None:
    instance, _ = make_provider([])
    assert instance.login_methods() == []


# -- healthcheck ----------------------------------------------------------------


async def test_healthcheck_oidc_ok(make_provider: Any, oidc_server: Any) -> None:
    oidc_server.configure_oidc()
    instance, _ = make_provider(
        [{"name": "corp", "issuer": oidc_server.base_url, "client_id": "c", "client_secret": "s"}]
    )
    await instance.healthcheck()  # discovery + JWKS fetched; probe kid absent is fine


async def test_healthcheck_oidc_unreachable_raises(make_provider: Any, oidc_server: Any) -> None:
    # Discovery not configured -> 404 -> JwksFetchError propagates (boot fails loud).
    instance, _ = make_provider(
        [{"name": "corp", "issuer": oidc_server.base_url, "client_id": "c", "client_secret": "s"}]
    )
    with pytest.raises(JwksFetchError):
        await instance.healthcheck()


async def test_healthcheck_github_ok(make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    oidc_server.set_route("/ghauth", status=200)
    monkeypatch.setattr(provider_mod, "GITHUB_AUTHORIZE_URL", f"{oidc_server.base_url}/ghauth")
    instance, _ = make_provider([{"name": "github", "preset": "github", "client_id": "c", "client_secret": "s"}])
    await instance.healthcheck()


async def test_healthcheck_github_unhealthy_raises(
    make_provider: Any, oidc_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    oidc_server.set_route("/ghauth", status=500)
    monkeypatch.setattr(provider_mod, "GITHUB_AUTHORIZE_URL", f"{oidc_server.base_url}/ghauth")
    instance, _ = make_provider([{"name": "github", "preset": "github", "client_id": "c", "client_secret": "s"}])
    with pytest.raises(RuntimeError, match="GitHub authorize endpoint unhealthy"):
        await instance.healthcheck()


# -- registration ---------------------------------------------------------------


def test_registered_in_both_registries() -> None:
    assert get_accounts_provider_factory("accounts-oidc") is OidcAccountsProvider
    assert get_identity_provider_factory("accounts-oidc") is OidcAccountsProvider
