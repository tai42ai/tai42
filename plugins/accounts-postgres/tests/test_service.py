"""Shared plumbing: the settings holder, token/id minting, and session helpers."""

from __future__ import annotations

import pytest

from tai42_accounts_postgres import service

from .conftest import FakeProviderSettings, record_provider_settings

# -- settings holder ------------------------------------------------------------


def test_provider_settings_raises_before_populated():
    # The autouse reset clears the active provider, so no provider is active this epoch.
    assert service.provider_settings_populated() is False
    with pytest.raises(RuntimeError, match="no active provider"):
        service.provider_settings()


def test_provider_settings_returns_after_populated():
    settings = FakeProviderSettings(redis=object(), admin=object())
    record_provider_settings(settings)
    assert service.provider_settings_populated() is True
    assert service.provider_settings() is settings


# -- minting / helpers ----------------------------------------------------------


def test_token_prefixes_and_paths():
    assert service.new_session_token().startswith("tai-sess-")
    assert service.new_invite_token().startswith("tai-inv-")
    assert service.normalize_email("  A@B.C  ") == "a@b.c"
    assert service.invite_login_path("tok") == "/login?invite=tok"


def test_too_many_attempts_message_pluralizes():
    assert service.too_many_attempts_message(30) == "Too many attempts — try again in 1 minute"
    assert service.too_many_attempts_message(120) == "Too many attempts — try again in 2 minutes"


async def test_mint_session_writes_and_returns_raw(monkeypatch, sessions_store):
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    raw = await service.mint_session("usr-1")
    assert raw.startswith("tai-sess-")
    assert service.token_hash(raw) in sessions_store.rows
