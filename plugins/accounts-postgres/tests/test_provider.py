"""PostgresAccountsProvider behavior against the in-memory store fakes."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.registry import get_identity_provider_factory
from tai42_contract.accounts import FormMethod
from tai42_contract.accounts.registry import get_accounts_provider_factory
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient

from tai42_accounts_postgres import provider as provider_module
from tai42_accounts_postgres import service
from tai42_accounts_postgres.provider import PostgresAccountsProvider
from tai42_accounts_postgres.settings import accounts_settings

from .conftest import FakeProviderSettings, ScriptedPg, future, make_pg_ctx, past


def _all_tables_present() -> ScriptedPg:
    return ScriptedPg(fetches=[[("accounts_users",), ("accounts_sessions",), ("accounts_invites",)]])


def _provider(redis=None, admin=None) -> PostgresAccountsProvider:
    return PostgresAccountsProvider(FakeProviderSettings(redis=redis, admin=admin))


def _seed_user(users, sessions, *, disabled=False, last_seen=None, absolute=None) -> str:
    users.rows["usr-1"] = {
        "user_id": "usr-1",
        "email": "a@b.c",
        "password_hash": "hash",
        "role": "admin",
        "disabled": disabled,
        "created_at": future(0),
    }
    raw = service.new_session_token()
    th = service.token_hash(raw)
    sessions.rows[th] = {
        "user_id": "usr-1",
        "last_seen_at": last_seen if last_seen is not None else future(0),
        "absolute_expires_at": absolute if absolute is not None else future(),
    }
    return raw


# -- registration ---------------------------------------------------------------


def test_registered_in_both_registries():
    assert get_accounts_provider_factory("accounts-postgres") is PostgresAccountsProvider
    assert get_identity_provider_factory("accounts-postgres") is PostgresAccountsProvider


# -- validate_token -------------------------------------------------------------


async def test_validate_token_prefix_rejects_without_db(monkeypatch):
    class Boom:
        async def resolve(self, token_hash):
            raise AssertionError("must not hit the store for a non-session token")

    monkeypatch.setattr(service, "sessions_store", lambda: Boom())
    assert await _provider().validate_token("sk-not-a-session") is None


async def test_validate_token_unknown_returns_none(monkeypatch, users_store, sessions_store):
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    assert await _provider().validate_token("tai-sess-nope") is None


async def test_validate_token_happy_returns_identity_and_touches(monkeypatch, users_store, sessions_store):
    raw = _seed_user(users_store, sessions_store, last_seen=past(120))
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    th = service.token_hash(raw)
    before = sessions_store.rows[th]["last_seen_at"]
    identity = await _provider().validate_token(raw)
    assert isinstance(identity, AuthIdentity)
    assert identity.user_id == "usr-1"
    assert identity.claims == {"email": "a@b.c", "role": "admin", "kind": "session"}
    # last_seen was >60s stale, so it was touched forward.
    assert sessions_store.rows[th]["last_seen_at"] > before


async def test_validate_token_fresh_is_not_touched(monkeypatch, users_store, sessions_store):
    raw = _seed_user(users_store, sessions_store, last_seen=past(5))
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    th = service.token_hash(raw)
    before = sessions_store.rows[th]["last_seen_at"]
    await _provider().validate_token(raw)
    assert sessions_store.rows[th]["last_seen_at"] == before


async def test_validate_token_disabled_returns_none_and_keeps_row(monkeypatch, users_store, sessions_store, caplog):
    raw = _seed_user(users_store, sessions_store, disabled=True)
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    th = service.token_hash(raw)
    with caplog.at_level("INFO"):
        assert await _provider().validate_token(raw) is None
    assert th in sessions_store.rows  # left for a possible re-enable
    assert "disabled user usr-1" in caplog.text


async def test_validate_token_absolute_expired_deletes(monkeypatch, users_store, sessions_store):
    raw = _seed_user(users_store, sessions_store, absolute=past(10))
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    th = service.token_hash(raw)
    assert await _provider().validate_token(raw) is None
    assert th not in sessions_store.rows


async def test_validate_token_idle_expired_deletes(monkeypatch, users_store, sessions_store):
    raw = _seed_user(users_store, sessions_store, last_seen=past(accounts_settings().session_idle_seconds + 10))
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    th = service.token_hash(raw)
    assert await _provider().validate_token(raw) is None
    assert th not in sessions_store.rows


async def test_validate_token_store_error_raises(monkeypatch, sessions_store):
    async def boom(token_hash):
        raise RuntimeError("pg down")

    sessions_store.resolve = boom
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    with pytest.raises(RuntimeError, match="pg down"):
        await _provider().validate_token("tai-sess-x")


# -- needs_bootstrap ------------------------------------------------------------


async def test_needs_bootstrap_tracks_count(monkeypatch, users_store):
    monkeypatch.setattr(service, "users_store", lambda: users_store)
    assert await _provider().needs_bootstrap() is True
    users_store.rows["usr-1"] = {
        "user_id": "usr-1",
        "email": "a",
        "password_hash": None,
        "role": "admin",
        "disabled": False,
        "created_at": future(0),
    }
    assert await _provider().needs_bootstrap() is False


# -- revoke_session -------------------------------------------------------------


async def test_revoke_session_variants(monkeypatch, users_store, sessions_store):
    raw = _seed_user(users_store, sessions_store)
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    provider = _provider()
    assert await provider.revoke_session("sk-foreign") is False
    assert await provider.revoke_session("tai-sess-unknown") is False
    assert await provider.revoke_session(raw) is True


async def test_revoke_session_store_error_raises(monkeypatch, sessions_store):
    async def boom(token_hash):
        raise RuntimeError("pg down")

    sessions_store.delete = boom
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    with pytest.raises(RuntimeError, match="pg down"):
        await _provider().revoke_session("tai-sess-x")


# -- login_methods --------------------------------------------------------------


def test_login_methods_declares_three_with_bootstrap_token_field():
    methods = [m for m in _provider().login_methods() if isinstance(m, FormMethod)]
    by_purpose = {m.purpose: m for m in methods}
    assert set(by_purpose) == {"login", "bootstrap", "invite"}
    assert by_purpose["login"].submit_path == "/api/login/password"
    field_names = {f.name for f in by_purpose["bootstrap"].fields}
    assert "bootstrap_token" in field_names


def test_login_methods_omits_token_field_when_open(monkeypatch):
    monkeypatch.setenv("TAI_ACCOUNTS_BOOTSTRAP_OPEN", "true")
    accounts_settings.cache_clear()
    methods = [m for m in _provider().login_methods() if isinstance(m, FormMethod)]
    bootstrap = next(m for m in methods if m.purpose == "bootstrap")
    assert "bootstrap_token" not in {f.name for f in bootstrap.fields}


# -- readiness_targets ----------------------------------------------------------


# -- healthcheck ----------------------------------------------------------------


async def test_healthcheck_missing_schema_raises_with_apply_command(monkeypatch):
    # Only one of the three tables present -> schema guard fires.
    monkeypatch.setattr(provider_module, "client_ctx", make_pg_ctx(ScriptedPg(fetches=[[("accounts_users",)]])))
    with pytest.raises(RuntimeError, match="schema missing"):
        await _provider().healthcheck()


async def test_healthcheck_gated_fixes_bootstrap_token(monkeypatch):
    monkeypatch.setattr(provider_module, "client_ctx", make_pg_ctx(_all_tables_present()))
    called: list[Any] = []

    async def record(redis_settings):
        called.append(redis_settings)

    monkeypatch.setattr(service, "ensure_bootstrap_token", record)
    redis = object()
    await _provider(redis=redis).healthcheck()
    assert called == [redis]


async def test_healthcheck_open_window_warns(monkeypatch, users_store, caplog):
    monkeypatch.setenv("TAI_ACCOUNTS_BOOTSTRAP_OPEN", "true")
    accounts_settings.cache_clear()
    monkeypatch.setattr(provider_module, "client_ctx", make_pg_ctx(_all_tables_present()))
    monkeypatch.setattr(service, "users_store", lambda: users_store)  # empty -> needs bootstrap
    with caplog.at_level("WARNING"):
        await _provider().healthcheck()
    assert "first-owner bootstrap is OPEN" in caplog.text


def test_readiness_targets_names_both_stores():
    redis_settings = object()
    provider = _provider(redis=redis_settings)
    pg_target, redis_target = provider.readiness_targets()
    assert pg_target.name == "accounts"
    assert pg_target.client is PostgresClient
    assert redis_target.name == "accounts"
    assert redis_target.client is RedisClient
    assert redis_target.settings is redis_settings
