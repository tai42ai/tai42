"""PostgresAccountsProvider behavior against the in-memory store fakes."""

from __future__ import annotations

import pytest
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.registry import get_identity_provider_factory
from tai42_contract.accounts import FormMethod, InviteCredential, PasswordCredential
from tai42_contract.accounts.errors import LoginAttachError
from tai42_contract.accounts.registry import get_accounts_provider_factory
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient

from tai42_accounts_postgres import provider as provider_module
from tai42_accounts_postgres import service
from tai42_accounts_postgres.db import SchemaOutOfDateError
from tai42_accounts_postgres.provider import PostgresAccountsProvider
from tai42_accounts_postgres.settings import accounts_settings
from tai42_accounts_postgres.stores import EmailTakenError, LoginExistsError

from .conftest import FakeProviderSettings, future, past


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


# -- attach_login ---------------------------------------------------------------


async def test_attach_login_password_sets_login(wire):
    result = await _provider().attach_login(
        "usr-owner", credential=PasswordCredential(email="Owner@X.Y", password="owner-password")
    )
    assert result.attached is True
    assert result.invite_token is None
    assert result.login_path is None
    row = wire.users.rows["usr-owner"]
    assert row["email"] == "owner@x.y"  # normalized before storage
    assert row["password_hash"] is not None
    assert row["role"] == "admin"  # the owner's admin role, mirrored on the login row


async def test_attach_login_invite_mints_link(wire):
    result = await _provider().attach_login("usr-owner", credential=InviteCredential(email="owner@x.y"))
    assert result.attached is False
    token = result.invite_token
    assert token is not None
    assert token.startswith("tai-inv-")
    assert result.login_path == f"/login?invite={token}"
    row = wire.users.rows["usr-owner"]
    assert row["password_hash"] is None  # password set later, on invite accept
    assert row["role"] == "admin"
    assert service.token_hash(token) in wire.invites.rows


async def test_attach_login_existing_login_raises(wire):
    wire.users.rows["usr-owner"] = {
        "user_id": "usr-owner",
        "email": "other@x.y",
        "password_hash": "h",
        "role": "admin",
        "disabled": False,
        "created_at": future(0),
    }
    with pytest.raises(LoginExistsError):
        await _provider().attach_login(
            "usr-owner", credential=PasswordCredential(email="new@x.y", password="owner-password")
        )


async def test_attach_login_email_taken_raises(wire):
    wire.users.rows["usr-other"] = {
        "user_id": "usr-other",
        "email": "taken@x.y",
        "password_hash": "h",
        "role": "viewer",
        "disabled": False,
        "created_at": future(0),
    }
    with pytest.raises(EmailTakenError):
        await _provider().attach_login(
            "usr-owner", credential=PasswordCredential(email="taken@x.y", password="owner-password")
        )


async def test_attach_login_password_too_short_raises(wire):
    with pytest.raises(LoginAttachError, match="at least 10"):
        await _provider().attach_login("usr-owner", credential=PasswordCredential(email="owner@x.y", password="short"))
    # Nothing was written — the length guard runs before the login row is created.
    assert wire.users.rows == {}


async def test_attach_login_invite_mint_failure_rolls_back_login(wire, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("invite boom")

    monkeypatch.setattr(wire.invites, "create", boom)
    with pytest.raises(RuntimeError, match="invite boom"):
        await _provider().attach_login("usr-owner", credential=InviteCredential(email="owner@x.y"))
    # The just-created login row was dropped, so the owner attach stays re-runnable.
    assert wire.users.rows == {}


# -- has_login ------------------------------------------------------------------


async def test_has_login_true_after_attach(wire):
    await _provider().attach_login(
        "usr-owner", credential=PasswordCredential(email="owner@x.y", password="owner-password")
    )
    assert await _provider().has_login("usr-owner") is True


async def test_has_login_false_when_no_row(wire):
    assert await _provider().has_login("nobody") is False


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


def test_login_methods_declares_password_and_invite():
    methods = [m for m in _provider().login_methods() if isinstance(m, FormMethod)]
    by_purpose = {m.purpose: m for m in methods}
    assert set(by_purpose) == {"login", "invite"}
    assert by_purpose["login"].submit_path == "/api/login/password"
    assert by_purpose["invite"].submit_path == "/api/login/invite/accept"


def test_login_methods_submit_path_follows_remapped_mount_base(monkeypatch):
    # An operator-remapped login route base moves the submit paths; each resolves
    # through the router's captured mount, not a hardcoded default.
    from tai42_accounts_postgres import routes_login

    monkeypatch.setattr(routes_login, "_MOUNT_BASE", "/api/sign-in")
    methods = [m for m in _provider().login_methods() if isinstance(m, FormMethod)]
    by_purpose = {m.purpose: m for m in methods}
    assert by_purpose["login"].submit_path == "/api/sign-in/password"
    assert by_purpose["invite"].submit_path == "/api/sign-in/invite/accept"


# -- healthcheck ----------------------------------------------------------------


async def _gate_ok() -> None:
    return None


async def test_healthcheck_refuses_on_out_of_date_schema(monkeypatch):
    # The boot gate's refusal (a pending migration or checksum mismatch) propagates
    # out of the healthcheck naming the fix — boot never proceeds on a stale schema.
    async def _refuse() -> None:
        raise SchemaOutOfDateError("database schema is out of date — refusing to start. Run 'tai db migrate'")

    monkeypatch.setattr(provider_module, "assert_accounts_schema_applied", _refuse)
    with pytest.raises(SchemaOutOfDateError, match="tai db migrate"):
        await _provider().healthcheck()


async def test_healthcheck_passes_when_schema_applied(monkeypatch):
    # A fully-applied schema chain lets boot proceed: the healthcheck is exactly the
    # schema gate now.
    monkeypatch.setattr(provider_module, "assert_accounts_schema_applied", _gate_ok)
    await _provider().healthcheck()


def test_readiness_targets_names_both_stores():
    redis_settings = object()
    provider = _provider(redis=redis_settings)
    pg_target, redis_target = provider.readiness_targets()
    assert pg_target.name == "accounts"
    assert pg_target.client is PostgresClient
    assert redis_target.name == "accounts"
    assert redis_target.client is RedisClient
    assert redis_target.settings is redis_settings
