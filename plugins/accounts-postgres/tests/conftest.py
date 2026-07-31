"""Test seams for tai42-accounts-postgres.

Three fakes stand in for the plugin's I/O: ``ScriptedPg`` (a psycopg-shaped seam
recording SQL and replaying fetches, so ``stores.py`` runs without a live DB),
``FakeRedis`` (the rate limiter / bootstrap token operations), and the in-memory
``Fake*Store`` / ``FakeAdminServices`` for the provider and route tests. The
``tai42_app`` handle is bound to a no-op fake at import so the route decorators
register cleanly under test.
"""

from __future__ import annotations

import json
import types
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from starlette.requests import Request

# -- bind a no-op app handle before any route module imports --------------------


class _FakeHttp:
    def custom_route(self, *args: Any, **kwargs: Any):
        def _decorator(func):
            return func

        return _decorator


class _FakeLifecycle:
    def __init__(self) -> None:
        self.startup_handlers: list[Any] = []

    def on_startup(self, func):
        self.startup_handlers.append(func)
        return func


class _FakeApp:
    def __init__(self) -> None:
        self.http = _FakeHttp()
        self.lifecycle = _FakeLifecycle()


_fake_app = _FakeApp()

from tai42_contract.app import tai42_app  # noqa: E402

tai42_app.bind(_fake_app)


# -- a psycopg-shaped seam for stores.py ----------------------------------------


class FakeUniqueViolation(psycopg.errors.UniqueViolation):
    """A ``UniqueViolation`` with a programmable ``.diag.constraint_name`` to drive
    the email-taken / id-collision split."""

    def __init__(self, constraint_name: str) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self._constraint = constraint_name

    @property
    def diag(self) -> Any:  # type: ignore[override]
        return types.SimpleNamespace(constraint_name=self._constraint)


class FakeCursor:
    def __init__(self, pg: ScriptedPg) -> None:
        self._pg = pg
        self.rowcount = 0

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self._pg.executed.append((sql, params))
        error = self._pg.pop_error()
        if error is not None:
            raise error
        self.rowcount = self._pg.rowcount

    async def fetchone(self) -> Any:
        return self._pg.pop_fetch()

    async def fetchall(self) -> list[Any]:
        value = self._pg.pop_fetch()
        return value if value is not None else []


class FakeConn:
    def __init__(self, pg: ScriptedPg) -> None:
        self._pg = pg

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        return FakeCursor(self._pg)

    def transaction(self) -> FakeConn:
        return self

    async def execute(self, sql: str, params: Any = None) -> None:
        self._pg.executed.append((sql, params))
        error = self._pg.pop_error()
        if error is not None:
            raise error


class ScriptedPg:
    """A pool stand-in. ``fetches`` are replayed by fetchone/fetchall in order;
    ``errors`` are raised by execute in order (``None`` entries pass)."""

    def __init__(
        self,
        fetches: Sequence[Any] | None = None,
        errors: Sequence[Exception | None] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._fetches = list(fetches or [])
        self._errors = list(errors or [])
        self.rowcount = rowcount
        self.executed: list[tuple[str, Any]] = []

    def pop_fetch(self) -> Any:
        return self._fetches.pop(0) if self._fetches else None

    def pop_error(self) -> Exception | None:
        return self._errors.pop(0) if self._errors else None

    def connection(self) -> FakeConn:
        return FakeConn(self)


def make_pg_ctx(pg: ScriptedPg):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield pg

    return _ctx


# -- a redis seam for the rate limiter + bootstrap token ------------------------


class FakeRedis:
    """The string/counter operations the plugin's Redis paths use."""

    def __init__(self, *, raise_on: Exception | None = None) -> None:
        self._values: dict[str, str] = {}
        self._ttls: dict[str, int] = {}
        self._raise_on = raise_on

    def _guard(self) -> None:
        if self._raise_on is not None:
            raise self._raise_on

    async def incr(self, key: str) -> int:
        self._guard()
        value = int(self._values.get(key, "0")) + 1
        self._values[key] = str(value)
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        self._guard()
        self._ttls[key] = ttl
        return True

    async def ttl(self, key: str) -> int:
        self._guard()
        return self._ttls.get(key, -1)

    async def get(self, key: str) -> str | None:
        self._guard()
        return self._values.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self._guard()
        if nx and key in self._values:
            return None
        self._values[key] = str(value)
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self._guard()
        removed = 0
        for key in keys:
            if self._values.pop(key, None) is not None:
                removed += 1
            self._ttls.pop(key, None)
        return removed


def make_redis_ctx(fake: FakeRedis):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    return _ctx


# -- in-memory store fakes for provider/route tests -----------------------------


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeAdminGuard:
    """Mirrors ``_AdminGuard``: the last-admin count and mutation over the same
    in-memory rows, as one 'transaction' the guard hook can race."""

    def __init__(self, store: FakeUsersStore) -> None:
        self._store = store

    async def read_target(self, user_id: str) -> dict[str, Any] | None:
        row = self._store.rows.get(user_id)
        return None if row is None else {"role": row["role"], "disabled": row["disabled"]}

    async def count_other_enabled_admins(self, user_id: str) -> int:
        return sum(
            1 for uid, r in self._store.rows.items() if uid != user_id and r["role"] == "admin" and not r["disabled"]
        )

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._store.rows[user_id]["disabled"] = disabled

    async def set_role(self, user_id: str, role: str) -> None:
        self._store.rows[user_id]["role"] = role

    async def delete(self, user_id: str) -> None:
        self._store.rows.pop(user_id, None)

    async def delete_sessions_for_user(self, user_id: str) -> None:
        # Delegate to the wired sessions store so the same rows disappear.
        from tai42_accounts_postgres import service

        await service.sessions_store().delete_for_user(user_id)

    async def delete_invites_for_user(self, user_id: str) -> None:
        from tai42_accounts_postgres import service

        await service.invites_store().delete_for_user(user_id)


class FakeUsersStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.owner_lock_hook: Any = None
        # Fires once on guarded-txn "lock acquire" to simulate a concurrent removal
        # committing before this one re-counts.
        self.admin_guard_hook: Any = None

    @asynccontextmanager
    async def admin_guard_txn(self):
        if self.admin_guard_hook is not None:
            hook, self.admin_guard_hook = self.admin_guard_hook, None
            hook(self)
        yield _FakeAdminGuard(self)

    async def create(self, user_id: str, email: str, role: str, password_hash: str | None = None) -> str:
        if any(r["email"] == email for r in self.rows.values()):
            from tai42_accounts_postgres.stores import EmailTakenError

            raise EmailTakenError(email)
        self.rows[user_id] = {
            "user_id": user_id,
            "email": email,
            "password_hash": password_hash,
            "role": role,
            "disabled": False,
            "created_at": _now(),
        }
        return user_id

    async def create_owner_if_first(
        self, user_id: str, email: str, password_hash: str, role: str
    ) -> dict[str, Any] | None:
        if self.owner_lock_hook is not None:
            hook, self.owner_lock_hook = self.owner_lock_hook, None
            hook(self)
        if self.rows:
            return None
        self.rows[user_id] = {
            "user_id": user_id,
            "email": email,
            "password_hash": password_hash,
            "role": role,
            "disabled": False,
            "created_at": _now(),
        }
        return dict(self.rows[user_id])

    async def get_by_email(self, email: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if row["email"] == email:
                return dict(row)
        return None

    async def get_by_user_id(self, user_id: str) -> dict[str, Any] | None:
        row = self.rows.get(user_id)
        return dict(row) if row is not None else None

    async def list(self) -> list[dict[str, Any]]:
        return [
            {
                "user_id": r["user_id"],
                "email": r["email"],
                "role": r["role"],
                "disabled": r["disabled"],
                "created_at": r["created_at"],
                "pending_invite": r["password_hash"] is None,
            }
            for r in self.rows.values()
        ]

    async def set_password_hash(self, user_id: str, password_hash: str) -> None:
        self.rows[user_id]["password_hash"] = password_hash

    async def set_role(self, user_id: str, role: str) -> None:
        self.rows[user_id]["role"] = role

    async def set_disabled(self, user_id: str, disabled: bool) -> None:
        self.rows[user_id]["disabled"] = disabled

    async def delete(self, user_id: str) -> None:
        self.rows.pop(user_id, None)

    async def count(self) -> int:
        return len(self.rows)

    async def count_other_enabled_admins(self, user_id: str) -> int:
        return sum(1 for uid, r in self.rows.items() if uid != user_id and r["role"] == "admin" and not r["disabled"])


class FakeSessionsStore:
    def __init__(self, users: FakeUsersStore) -> None:
        self._users = users
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(self, token_hash: str, user_id: str, absolute_expires_at: Any) -> None:
        self.rows[token_hash] = {
            "user_id": user_id,
            "last_seen_at": _now(),
            "absolute_expires_at": absolute_expires_at,
        }

    async def resolve(self, token_hash: str) -> dict[str, Any] | None:
        row = self.rows.get(token_hash)
        if row is None:
            return None
        user = self._users.rows.get(row["user_id"])
        if user is None:
            return None
        return {
            "user_id": row["user_id"],
            "email": user["email"],
            "role": user["role"],
            "disabled": user["disabled"],
            "last_seen_at": row["last_seen_at"],
            "absolute_expires_at": row["absolute_expires_at"],
        }

    async def touch(self, token_hash: str, now: Any) -> None:
        if token_hash in self.rows:
            self.rows[token_hash]["last_seen_at"] = now

    async def delete(self, token_hash: str) -> bool:
        return self.rows.pop(token_hash, None) is not None

    async def delete_for_user(self, user_id: str, keep_token_hash: str | None = None) -> None:
        for th in list(self.rows):
            if self.rows[th]["user_id"] == user_id and th != keep_token_hash:
                del self.rows[th]


class FakeInvitesStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(self, token_hash: str, user_id: str, expires_at: Any) -> None:
        for th in list(self.rows):
            if self.rows[th]["user_id"] == user_id:
                del self.rows[th]
        self.rows[token_hash] = {"user_id": user_id, "expires_at": expires_at, "consumed_at": None}

    async def consume(self, token_hash: str, now: Any) -> str | None:
        row = self.rows.get(token_hash)
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
            return None
        row["consumed_at"] = now
        return row["user_id"]

    async def delete_for_user(self, user_id: str) -> None:
        for th in list(self.rows):
            if self.rows[th]["user_id"] == user_id:
                del self.rows[th]


@dataclass
class FakeAdminServices:
    """Records the injected policy-service calls; can be told to fail apply_role.

    ``known_roles`` mirrors the real ``apply_role``: a role name outside it raises
    ``KeyError`` before writing any policy. ``None`` accepts every name.
    """

    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail_apply_role: bool = False
    known_roles: set[str] | None = None

    async def apply_role(self, user_id: str, role: str) -> None:
        self.calls.append(("apply_role", user_id, role))
        if self.known_roles is not None and role not in self.known_roles:
            raise KeyError(f"unknown role: {role!r}")
        if self.fail_apply_role:
            raise RuntimeError("apply_role boom")

    async def remove_policy(self, user_id: str) -> None:
        self.calls.append(("remove_policy", user_id))

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        self.calls.append(("set_user_disabled", user_id, str(disabled)))


@dataclass
class FakeProviderSettings:
    redis: Any = None
    admin: Any = None


def future(seconds: int = 3600) -> datetime:
    return _now() + timedelta(seconds=seconds)


def past(seconds: int = 3600) -> datetime:
    return _now() - timedelta(seconds=seconds)


def build_request(
    body: Any = None,
    *,
    method: str = "POST",
    client: tuple[str, int] | None = ("198.51.100.7", 40000),
    headers: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
) -> Request:
    """A Starlette ``Request`` with a JSON body, client peer, headers, and path
    params — enough to drive a route handler directly."""
    payload = json.dumps(body).encode() if body is not None else b""
    header_items = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "headers": header_items,
        "client": client,
        "path": "/",
        "query_string": b"",
        "path_params": path_params or {},
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, _receive)


def response_json(response: Any) -> Any:
    return json.loads(bytes(response.body))


# -- isolation fixtures ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Snapshot + restore both module-level registries around each test so a
    registration never leaks into the next."""
    from tai42_contract.access_control import registry as identity_registry
    from tai42_contract.accounts import registry as accounts_registry

    identity_saved = dict(identity_registry._REGISTRY)
    accounts_saved = dict(accounts_registry._REGISTRY)
    try:
        yield
    finally:
        identity_registry._REGISTRY.clear()
        identity_registry._REGISTRY.update(identity_saved)
        accounts_registry._REGISTRY.clear()
        accounts_registry._REGISTRY.update(accounts_saved)


@pytest.fixture(autouse=True)
def _reset_plugin_state():
    """Reset the settings cache, the hash gate, and the settings holder so an
    env-dependent test starts clean and never leaks into the next."""
    from tai42_accounts_postgres import hashing, service
    from tai42_accounts_postgres.settings import accounts_settings

    accounts_settings.cache_clear()
    hashing.reset_hash_gate()
    service.reset_provider_settings()
    yield
    accounts_settings.cache_clear()
    hashing.reset_hash_gate()
    service.reset_provider_settings()


@pytest.fixture
def users_store() -> FakeUsersStore:
    return FakeUsersStore()


@pytest.fixture
def sessions_store(users_store: FakeUsersStore) -> FakeSessionsStore:
    return FakeSessionsStore(users_store)


@pytest.fixture
def invites_store() -> FakeInvitesStore:
    return FakeInvitesStore()


@pytest.fixture
def admin() -> FakeAdminServices:
    return FakeAdminServices()


@pytest.fixture
def wire(monkeypatch, users_store, sessions_store, invites_store, admin):
    """Wire the in-memory stores + injected services into the plugin and populate
    the settings holder. Returns the pieces for assertions."""
    from tai42_accounts_postgres import service

    monkeypatch.setattr(service, "users_store", lambda: users_store)
    monkeypatch.setattr(service, "sessions_store", lambda: sessions_store)
    monkeypatch.setattr(service, "invites_store", lambda: invites_store)
    settings = FakeProviderSettings(redis=object(), admin=admin)
    service.set_provider_settings(settings)
    return types.SimpleNamespace(
        users=users_store,
        sessions=sessions_store,
        invites=invites_store,
        admin=admin,
        settings=settings,
    )
