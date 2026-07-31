"""Tests for settings parsing and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_accounts_oidc.settings import OidcAccountsSettings


def _provider(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"name": "p", "client_id": "cid", "client_secret": "secret"}
    base.update(over)
    return base


def _settings(**over: object) -> OidcAccountsSettings:
    base: dict[str, object] = {"state_key": "k", "public_base_url": "https://studio.example.com"}
    base.update(over)
    return OidcAccountsSettings(**base)  # type: ignore[arg-type]


def test_defaults_and_provider_parsing() -> None:
    settings = _settings(providers=[_provider(name="google", preset="google")])
    assert settings.session_idle_seconds == 86400
    assert settings.session_absolute_seconds == 2592000
    assert len(settings.providers) == 1
    assert settings.providers[0].scopes == ["openid", "email", "profile"]
    assert settings.providers[0].claim == "sub"


def test_empty_providers_default() -> None:
    assert _settings().providers == []


def test_duplicate_names_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate provider name"):
        _settings(
            providers=[
                _provider(name="dup", preset="google"),
                _provider(name="dup", preset="google"),
            ]
        )


def test_bad_slug_name_rejected() -> None:
    with pytest.raises(ValidationError, match="must match"):
        _settings(providers=[_provider(name="Bad Name", preset="google")])


def test_unknown_preset_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown preset"):
        _settings(providers=[_provider(name="p", preset="facebook")])


def test_per_tenant_preset_without_issuer_rejected() -> None:
    with pytest.raises(ValidationError, match="no issuer"):
        _settings(providers=[_provider(name="corp", preset="okta")])


def test_public_base_url_https_required_non_loopback() -> None:
    with pytest.raises(ValidationError, match="must be https"):
        _settings(public_base_url="http://studio.example.com")


def test_public_base_url_http_loopback_allowed() -> None:
    settings = _settings(public_base_url="http://127.0.0.1:8000")
    assert settings.public_base_url == "http://127.0.0.1:8000"


def test_public_base_url_trailing_slash_stripped() -> None:
    settings = _settings(public_base_url="https://studio.example.com/")
    assert settings.public_base_url == "https://studio.example.com"


def test_public_base_url_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute URL"):
        _settings(public_base_url="/relative/path")


def test_state_key_and_base_url_optional_at_load() -> None:
    # The required-ness is enforced at provider construction, not settings load, so
    # a load with them absent must succeed (the None sentinel).
    settings = OidcAccountsSettings()  # type: ignore[call-arg]
    assert settings.state_key is None
    assert settings.public_base_url is None
