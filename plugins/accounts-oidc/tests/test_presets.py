"""Tests for provider preset resolution."""

from __future__ import annotations

import pytest

from tai42_accounts_oidc.presets import resolve_provider
from tai42_accounts_oidc.settings import OidcProviderConfig


def _config(**over: object) -> OidcProviderConfig:
    base: dict[str, object] = {"name": "p", "client_id": "cid", "client_secret": "secret"}
    base.update(over)
    return OidcProviderConfig(**base)  # type: ignore[arg-type]


def test_google_fixes_issuer_and_label() -> None:
    resolved = resolve_provider(_config(name="google", preset="google"))
    assert resolved.kind == "oidc"
    assert resolved.issuer == "https://accounts.google.com"
    assert resolved.label == "Continue with Google"
    assert resolved.claim == "sub"


def test_google_issuer_is_fixed_even_if_operator_sets_one() -> None:
    resolved = resolve_provider(_config(name="google", preset="google", issuer="https://evil.example"))
    assert resolved.issuer == "https://accounts.google.com"


@pytest.mark.parametrize("preset", ["auth0", "okta", "keycloak", "azure"])
def test_per_tenant_preset_requires_issuer(preset: str) -> None:
    with pytest.raises(ValueError, match="no issuer"):
        resolve_provider(_config(preset=preset))


@pytest.mark.parametrize("preset", ["auth0", "okta", "keycloak", "azure"])
def test_per_tenant_preset_with_issuer_resolves(preset: str) -> None:
    resolved = resolve_provider(_config(preset=preset, issuer="https://tenant.example"))
    assert resolved.issuer == "https://tenant.example"
    assert resolved.label.startswith("Continue with")


def test_raw_provider_requires_issuer() -> None:
    with pytest.raises(ValueError, match="no issuer"):
        resolve_provider(_config(preset=None))


def test_raw_provider_uses_generic_label() -> None:
    resolved = resolve_provider(_config(name="corp", issuer="https://corp.example"))
    assert resolved.label == "Sign in with corp"


def test_operator_label_wins() -> None:
    resolved = resolve_provider(_config(name="corp", issuer="https://corp.example", display={"label": "Corp SSO"}))
    assert resolved.label == "Corp SSO"


def test_github_is_plain_oauth2() -> None:
    resolved = resolve_provider(_config(name="github", preset="github"))
    assert resolved.kind == "github"
    assert resolved.issuer is None
    assert resolved.label == "Continue with GitHub"
    # GitHub maps identity by numeric id, not the OIDC sub, when left at default.
    assert resolved.claim == "id"


def test_github_respects_explicit_claim() -> None:
    resolved = resolve_provider(_config(name="github", preset="github", claim="login"))
    assert resolved.claim == "login"


def test_icon_passes_through() -> None:
    resolved = resolve_provider(_config(name="google", preset="google", display={"label": "G", "icon": "<svg/>"}))
    assert resolved.icon == "<svg/>"
