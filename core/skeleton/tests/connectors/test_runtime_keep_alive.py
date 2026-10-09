"""Use keeps a connection alive: a record served without a write gets its session expiry moved, at most once
per interval, by putting back the SAME sealed blob through the compare-and-set — never re-encrypting, so no
other writer loses its compare-and-set."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionRecord
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.oauth.client import TokenResponse
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.runtime.resolver import resolve_managed_auth

from .conftest import CID, make_noauth_stdio_descriptor, make_oauth_descriptor, make_oauth_record


class _Store:
    """Compare-and-set over the sealed blob, recording every put and its session expiry."""

    def __init__(self, record: ConnectionRecord) -> None:
        self.blob = crypto.encrypt(record.to_storage_json().encode(), aad=record.connection_id)
        self.puts: list[tuple[bytes, bytes | None, datetime | None]] = []
        self.committed = 0
        self.error: Exception | None = None

    def record(self) -> ConnectionRecord:
        return ConnectionRecord.model_validate_json(crypto.decrypt(self.blob, aad=CID))

    async def put(self, connection_id, blob, *, create_only=False, expected_blob=None, session_expires_at=None):
        self.puts.append((blob, expected_blob, session_expires_at))
        if self.error is not None:
            raise self.error
        if expected_blob is not None and expected_blob != self.blob:
            return False
        self.blob = blob
        self.committed += 1
        return True


class _Wiring:
    def __init__(self) -> None:
        self.store: _Store | None = None
        self.descriptor = make_oauth_descriptor()
        self.markers: set[str] = set()


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    refresh_owner.reset()
    resolver_mod.reset_keep_alive_stamps()
    yield
    resolver_mod.reset_keep_alive_stamps()


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch, oauth_client_env) -> _Wiring:
    w = _Wiring()

    async def _load(connection_id):
        assert w.store is not None
        return w.store.record(), w.store.blob

    @asynccontextmanager
    async def _lock(connection_id):
        yield

    async def _claim(cid, seconds):
        if cid in w.markers:
            return False
        w.markers.add(cid)
        return True

    async def _none(cid):
        return None

    async def _false(cid):
        return False

    monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
    monkeypatch.setattr(resolver_mod, "connection_lock", _lock)
    monkeypatch.setattr(resolver_mod, "token_store", lambda: w.store)
    monkeypatch.setattr(resolver_mod, "get_provider", lambda pid: w.descriptor)
    monkeypatch.setattr(resolver_mod, "claim_keep_alive", _claim)
    monkeypatch.setattr(resolver_mod, "refresh_cooldown_active", _false)
    monkeypatch.setattr(resolver_mod, "open_refresh_cooldown", _none)
    monkeypatch.setattr(resolver_mod, "clear_refresh_cooldown", _none)
    return w


def _oauth(*, refresh: bool = True, expires_in: int | None = 3600) -> ConnectionRecord:
    record = make_oauth_record(expires_in_seconds=expires_in or 3600)
    record.refresh_token = SecretStr("refresh-tok") if refresh else None
    if expires_in is None:
        record.access_token_expires_at = None
    return record


def _noauth() -> ConnectionRecord:
    return ConnectionRecord(
        connection_id=CID,
        provider_id="widgets",
        kind="none",
        alias="main",
        enabled_sub_services=["search"],
        config_values={"api_key": SecretStr("k")},
        auth_health_state=AuthHealthState.HEALTHY,
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("kind", ["no-auth", "no-refresh-token", "long-lived"])
async def test_a_record_served_twice_within_the_interval_is_written_once_with_a_later_expiry(
    wiring: _Wiring, kind: str
):
    if kind == "no-auth":
        wiring.descriptor = make_noauth_stdio_descriptor()
        record, provider, sub = _noauth(), "widgets", "search"
    elif kind == "no-refresh-token":
        record, provider, sub = _oauth(refresh=False, expires_in=None), "acme", "mail"
    else:
        record, provider, sub = _oauth(expires_in=30 * 24 * 3600), "acme", "mail"
    store = wiring.store = _Store(record)
    original = store.blob
    before = datetime.now(UTC)
    await resolve_managed_auth(CID, provider, sub)
    await resolve_managed_auth(CID, provider, sub)
    assert len(store.puts) == 1
    blob, expected, expires = store.puts[0]
    # The same sealed blob, byte for byte, as both the value and the compare-and-set handle.
    assert blob == original
    assert expected == original
    assert store.blob == original
    assert expires is not None
    assert expires > before + timedelta(days=179)


async def test_the_read_only_mode_never_writes(wiring: _Wiring):
    store = wiring.store = _Store(_oauth())
    assert await resolve_managed_auth(CID, "acme", "mail", allow_refresh=False) is not None
    wiring.descriptor = make_noauth_stdio_descriptor()
    store_noauth = wiring.store = _Store(_noauth())
    await resolve_managed_auth(CID, "widgets", "search", allow_refresh=False)
    assert store.puts == []
    assert store_noauth.puts == []


async def test_a_record_this_process_refreshed_within_the_interval_is_not_written_again(
    wiring: _Wiring, monkeypatch: pytest.MonkeyPatch
):
    store = wiring.store = _Store(_oauth(expires_in=10))

    async def _refresh(**kwargs):
        return TokenResponse(
            access_token="fresh",
            refresh_token="rt2",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes=[],
            raw={},
        )

    monkeypatch.setattr(oauth_client, "refresh", _refresh)
    await resolve_managed_auth(CID, "acme", "mail")  # refreshes: one write
    await resolve_managed_auth(CID, "acme", "mail")  # fresh now: no keep-alive write
    assert store.committed == 1
    assert len(store.puts) == 1


async def test_the_interval_is_half_the_session_cap_up_to_a_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTORS_MAX_SESSION_TTL", str(8 * 3600))
    reset_all_settings()
    assert resolver_mod._keep_alive_interval() == timedelta(hours=4)
    monkeypatch.setenv("CONNECTORS_MAX_SESSION_TTL", str(30 * 24 * 3600))
    reset_all_settings()
    assert resolver_mod._keep_alive_interval() == timedelta(hours=24)


async def test_two_processes_serving_the_same_record_cause_one_put(wiring: _Wiring):
    store = wiring.store = _Store(_oauth())
    await resolve_managed_auth(CID, "acme", "mail")
    resolver_mod.reset_keep_alive_stamps()  # a second process: its own map, the same marker
    await resolve_managed_auth(CID, "acme", "mail")
    assert len(store.puts) == 1


async def test_after_the_interval_the_record_is_written_again(wiring: _Wiring, monkeypatch: pytest.MonkeyPatch):
    store = wiring.store = _Store(_oauth())
    clock = [1000.0]
    monkeypatch.setattr(resolver_mod, "_monotonic", lambda: clock[0])
    await resolve_managed_auth(CID, "acme", "mail")
    clock[0] += resolver_mod._keep_alive_interval().total_seconds() + 1
    wiring.markers.clear()  # the marker lives exactly the interval
    await resolve_managed_auth(CID, "acme", "mail")
    assert len(store.puts) == 2


async def test_a_put_that_finds_the_blob_changed_raises_nothing(wiring: _Wiring, monkeypatch: pytest.MonkeyPatch):
    store = wiring.store = _Store(_oauth())
    loaded = store.blob

    async def _load(connection_id):
        record = store.record()
        store.blob = b"changed-meanwhile"
        return record, loaded

    monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
    auth = await resolve_managed_auth(CID, "acme", "mail")
    assert auth is not None
    assert store.committed == 0


async def test_a_store_error_on_the_put_is_logged_and_the_credential_is_served(wiring: _Wiring, caplog):
    store = wiring.store = _Store(_oauth())
    store.error = RuntimeError("store down")
    with caplog.at_level(logging.WARNING, logger=resolver_mod.__name__):
        auth = await resolve_managed_auth(CID, "acme", "mail")
    assert auth is not None
    assert auth.access_token == "access-tok"
    assert any("keep-alive write" in record.message for record in caplog.records)


async def test_a_redis_error_on_the_marker_still_writes(wiring: _Wiring, monkeypatch: pytest.MonkeyPatch):
    store = wiring.store = _Store(_oauth())

    async def _claim_after_error(cid, seconds):
        return True  # the marker's Redis error path claims the write (logged in the lock module)

    monkeypatch.setattr(resolver_mod, "claim_keep_alive", _claim_after_error)
    await resolve_managed_auth(CID, "acme", "mail")
    assert len(store.puts) == 1


async def test_a_refresh_that_loaded_before_a_keep_alive_still_commits_after_it(
    wiring: _Wiring, monkeypatch: pytest.MonkeyPatch
):
    store = wiring.store = _Store(_oauth(expires_in=10))

    async def _refresh_while_a_peer_keeps_alive(**kwargs):
        # Another caller serves the record (a keep-alive) while this refresh is in flight.
        resolver_mod.reset_keep_alive_stamps()
        record, blob = store.record(), store.blob
        await resolver_mod._keep_alive(CID, record, blob)
        return TokenResponse(
            access_token="rotated-at",
            refresh_token="rotated-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            granted_scopes=[],
            raw={},
        )

    monkeypatch.setattr(oauth_client, "refresh", _refresh_while_a_peer_keeps_alive)
    auth = await resolve_managed_auth(CID, "acme", "mail")
    assert auth is not None
    assert auth.access_token == "rotated-at"
    stored = store.record()
    assert stored.refresh_token is not None
    assert stored.refresh_token.get_secret_value() == "rotated-rt"
    assert len(store.puts) == 2  # the keep-alive, then the refresh's write-back — both committed
    assert store.committed == 2
