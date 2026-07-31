"""Shared plumbing: the settings holder, minting, compensation, bootstrap token."""

from __future__ import annotations

import pytest

from tai42_accounts_postgres import service
from tai42_accounts_postgres.settings import accounts_settings

from .conftest import FakeAdminServices, FakeProviderSettings, FakeRedis, make_redis_ctx

# -- settings holder ------------------------------------------------------------


def test_provider_settings_raises_before_populated():
    service.reset_provider_settings()
    assert service.provider_settings_populated() is False
    with pytest.raises(RuntimeError, match="ACCESS_CONTROL_ENABLE=true"):
        service.provider_settings()


def test_provider_settings_returns_after_populated():
    settings = FakeProviderSettings(redis=object(), admin=object())
    service.set_provider_settings(settings)
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


# -- apply_role compensation ----------------------------------------------------


async def test_apply_role_compensated_success():
    admin = FakeAdminServices()
    service.set_provider_settings(FakeProviderSettings(admin=admin))
    cleaned = False

    async def cleanup() -> None:
        nonlocal cleaned
        cleaned = True

    await service.apply_role_compensated("usr-1", "admin", cleanup)
    assert ("apply_role", "usr-1", "admin") in admin.calls
    assert cleaned is False


async def test_apply_role_compensated_failure_cleans_and_reraises():
    admin = FakeAdminServices(fail_apply_role=True)
    service.set_provider_settings(FakeProviderSettings(admin=admin))
    cleaned = False

    async def cleanup() -> None:
        nonlocal cleaned
        cleaned = True

    with pytest.raises(RuntimeError, match="apply_role boom"):
        await service.apply_role_compensated("usr-1", "admin", cleanup)
    assert cleaned is True


async def test_apply_role_compensated_cleanup_failure_preserves_both():
    admin = FakeAdminServices(fail_apply_role=True)
    service.set_provider_settings(FakeProviderSettings(admin=admin))

    async def cleanup() -> None:
        raise RuntimeError("cleanup boom")

    with pytest.raises(RuntimeError, match="residual accounts_users row 'usr-1'") as exc:
        await service.apply_role_compensated("usr-1", "admin", cleanup)
    # The original apply error is the cause; the cleanup failure is the context —
    # neither is discarded.
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert "apply_role boom" in str(exc.value.__cause__)
    assert "cleanup boom" in str(exc.value.__context__)


# -- bootstrap token ------------------------------------------------------------


async def test_ensure_bootstrap_token_writes_and_logs_when_gated(monkeypatch, caplog):
    fake = FakeRedis()
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(fake))
    with caplog.at_level("INFO"):
        await service.ensure_bootstrap_token(object())
    stored = await fake.get(service.bootstrap_token_redis_key())
    assert stored is not None
    assert "first-owner bootstrap token" in caplog.text


async def test_ensure_bootstrap_token_noop_when_open(monkeypatch):
    monkeypatch.setenv("TAI_ACCOUNTS_BOOTSTRAP_OPEN", "true")
    accounts_settings.cache_clear()
    fake = FakeRedis()
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(fake))
    await service.ensure_bootstrap_token(object())
    assert await fake.get(service.bootstrap_token_redis_key()) is None


async def test_ensure_bootstrap_token_noop_when_operator_token_set(monkeypatch):
    monkeypatch.setenv("TAI_ACCOUNTS_BOOTSTRAP_TOKEN", "op-token")
    accounts_settings.cache_clear()
    fake = FakeRedis()
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(fake))
    await service.ensure_bootstrap_token(object())
    assert await fake.get(service.bootstrap_token_redis_key()) is None


async def test_ensure_bootstrap_token_only_winner_logs(monkeypatch, caplog):
    fake = FakeRedis()
    await fake.set(service.bootstrap_token_redis_key(), "already-set")
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(fake))
    with caplog.at_level("INFO"):
        await service.ensure_bootstrap_token(object())
    assert "first-owner bootstrap token" not in caplog.text


async def test_resolve_bootstrap_token_operator_wins(monkeypatch):
    monkeypatch.setenv("TAI_ACCOUNTS_BOOTSTRAP_TOKEN", "op-token")
    accounts_settings.cache_clear()
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(FakeRedis()))
    assert await service.resolve_bootstrap_token(object()) == "op-token"


async def test_resolve_bootstrap_token_reads_shared_value(monkeypatch):
    fake = FakeRedis()
    await fake.set(service.bootstrap_token_redis_key(), "shared-token")
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(fake))
    assert await service.resolve_bootstrap_token(object()) == "shared-token"


async def test_resolve_bootstrap_token_absent_raises_fail_closed(monkeypatch):
    monkeypatch.setattr(service, "client_ctx", make_redis_ctx(FakeRedis()))
    with pytest.raises(RuntimeError, match="invariant breach"):
        await service.resolve_bootstrap_token(object())
