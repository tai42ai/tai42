"""The refresh of a grant: where it is refreshed, the issuer table, optional refresh token and expiry, and the
section that finishes whatever cancels its caller.

The token endpoint is a loopback protocol fixture; the store is an in-memory compare-and-set fake.
"""

from __future__ import annotations

import asyncio
import os
import time
import types
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_contract.connectors.providers import OAuthEndpoints, ProviderDescriptor
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.connectors.oauth import crypto, discovery
from tai42_skeleton.connectors.oauth.authorization import ClientSource
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorReconnectRequiredError,
    ConnectorRefreshFailingError,
    resolve_managed_auth,
)

from .conftest import CID, make_discovered_descriptor
from .oauth_fakes import FakeOAuthServer, Reply, serve

SECRET = "remote-env-secret"


class _Store:
    """The durable compare-and-set over the sealed blob, modelling the database and the cache writes."""

    def __init__(self, record: ConnectionRecord, *, write_delay: float = 0.0) -> None:
        self.blob = self.seal(record)
        self.write_delay = write_delay
        self.database: list[bytes] = []
        self.cache: list[bytes] = []
        self.puts: list[tuple[bytes, bytes | None]] = []

    @staticmethod
    def seal(record: ConnectionRecord) -> bytes:
        return crypto.encrypt(record.to_storage_json().encode(), connection_id=record.connection_id)

    def record(self) -> ConnectionRecord:
        return ConnectionRecord.model_validate_json(crypto.decrypt(self.blob, connection_id=CID))

    async def put(self, connection_id, blob, *, create_only=False, expected_blob=None, session_expires_at=None):
        self.puts.append((blob, expected_blob))
        if expected_blob is not None and expected_blob != self.blob:
            return False
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.database.append(blob)
        self.blob = blob
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.cache.append(blob)
        return True


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    monkeypatch.setenv("REMOTE_CLIENT_ID", "remote-env-id")
    monkeypatch.setenv("REMOTE_CLIENT_SECRET", SECRET)
    reset_all_settings()
    discovery.reset_discovery_cache()
    refresh_owner.reset()
    yield
    discovery.reset_discovery_cache()
    refresh_owner.reset()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


class _Wiring:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.store: _Store | None = None
        self.descriptor: ProviderDescriptor | None = None
        self.cooldown: set[str] = set()
        self.lock_gate: asyncio.Event | None = None

    def use(self, record: ConnectionRecord, descriptor: ProviderDescriptor, **store_kwargs) -> _Store:
        self.store = _Store(record, **store_kwargs)
        self.descriptor = descriptor
        return self.store


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> _Wiring:
    w = _Wiring(monkeypatch)

    async def _load(connection_id):
        assert w.store is not None
        return w.store.record(), w.store.blob

    @asynccontextmanager
    async def _lock(connection_id):
        if w.lock_gate is not None:
            await w.lock_gate.wait()
        yield

    async def _active(cid):
        return cid in w.cooldown

    async def _open(cid):
        w.cooldown.add(cid)

    async def _clear(cid):
        w.cooldown.discard(cid)

    async def _claim(cid, seconds):
        return True

    monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
    monkeypatch.setattr(resolver_mod, "connection_lock", _lock)
    monkeypatch.setattr(resolver_mod, "token_store", lambda: w.store)
    monkeypatch.setattr(resolver_mod, "get_provider", lambda pid: w.descriptor)
    monkeypatch.setattr(resolver_mod, "refresh_cooldown_active", _active)
    monkeypatch.setattr(resolver_mod, "open_refresh_cooldown", _open)
    monkeypatch.setattr(resolver_mod, "clear_refresh_cooldown", _clear)
    monkeypatch.setattr(resolver_mod, "claim_keep_alive", _claim)
    monkeypatch.setattr(resolver_mod, "TRANSIENT_BACKOFF_BASE_SECONDS", 0.0)
    resolver_mod.reset_keep_alive_stamps()
    return w


def _record(
    *,
    grant: ConnectionGrant | None,
    refresh_token: str | None = "rt-old",
    expires_in: float | None = -10,
    provider_id: str = "remote",
) -> ConnectionRecord:
    now = datetime.now(UTC)
    return ConnectionRecord(
        connection_id=CID,
        provider_id=provider_id,
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=["files"],
        granted_scopes=["files.read"],
        access_token=SecretStr("at-old"),
        refresh_token=SecretStr(refresh_token) if refresh_token else None,
        access_token_expires_at=None if expires_in is None else now + timedelta(seconds=expires_in),
        auth_health_state=AuthHealthState.HEALTHY,
        grant=grant,
        created_at=now,
    )


def _publish(fake: FakeOAuthServer, servers: tuple[str, ...] = ("/a",)) -> str:
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}{p}" for p in servers]})
    for path in servers:
        fake.authorization_server(path)
    return mcp


def _rotating(fake: FakeOAuthServer, path: str = "/a/token", *, delay: float = 0.0) -> None:
    fake.route(
        "POST",
        path,
        Reply(body={"access_token": "at-new", "refresh_token": "rt-rotated", "expires_in": 3600}, delay=delay),
    )


def _token_requests(fake: FakeOAuthServer) -> list:
    return [r for r in fake.received if r.path.endswith("/token")]


# -- where a grant is refreshed --------------------------------------------------------


async def test_a_grant_is_refreshed_at_its_server_with_its_resource(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake)
    grant = ConnectionGrant(issuer=f"{fake.base}/a", resource="https://published.example.com/mcp")
    store = wiring.use(_record(grant=grant), make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"))
    _rotating(fake)
    auth = await resolve_managed_auth(CID, "remote", "files")
    assert auth is not None
    assert auth.access_token == "at-new"
    (request,) = _token_requests(fake)
    assert request.form["resource"] == "https://published.example.com/mcp"
    assert request.form["client_id"] == "remote-env-id"
    assert request.form["refresh_token"] == "rt-old"
    stored = store.record()
    assert stored.refresh_token is not None
    assert stored.refresh_token.get_secret_value() == "rt-rotated"
    assert stored.auth_health_state is AuthHealthState.HEALTHY


async def test_a_record_without_a_grant_under_a_now_discovered_descriptor_refreshes_without_resource(
    wiring: _Wiring, fake: FakeOAuthServer
):
    mcp = _publish(fake)
    wiring.use(_record(grant=None), make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"))
    _rotating(fake)
    await resolve_managed_auth(CID, "remote", "files")
    (request,) = _token_requests(fake)
    assert "resource" not in request.form
    assert request.form["client_secret"] == SECRET


async def test_expired_with_no_refresh_token_requires_a_reconnect(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake)
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"), refresh_token=None),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
    )
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert _token_requests(fake) == []


async def test_a_short_lived_token_with_no_refresh_token_is_served_its_whole_life(
    wiring: _Wiring, fake: FakeOAuthServer
):
    mcp = _publish(fake)
    wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"), refresh_token=None, expires_in=30),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
    )
    auth = await resolve_managed_auth(CID, "remote", "files")
    assert auth is not None
    assert auth.access_token == "at-old"
    assert fake.received == []


async def test_a_token_with_no_expiry_is_served_without_a_refresh(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake)
    wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a"), refresh_token=None, expires_in=None),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
    )
    auth = await resolve_managed_auth(CID, "remote", "files")
    assert auth is not None
    assert auth.access_token == "at-old"
    assert fake.received == []


# -- the issuer table -------------------------------------------------------------------


async def test_a_server_that_adds_a_second_server_first_moves_no_grant(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake, servers=("/b", "/a"))
    store = wiring.use(_record(grant=ConnectionGrant(issuer=f"{fake.base}/a")), make_discovered_descriptor(mcp_url=mcp))
    monkey_client(wiring)
    _rotating(fake)
    await resolve_managed_auth(CID, "remote", "files")
    assert [r.path for r in _token_requests(fake)] == ["/a/token"]
    assert store.record().auth_health_state is AuthHealthState.HEALTHY


def monkey_client(wiring: _Wiring, *, source: ClientSource = "manual") -> None:
    """Serve the stored-client lookup a stored client (the store itself is a later seam)."""
    from tai42_skeleton.connectors.oauth import authorization

    async def _resolve_client(descriptor, resolved, *, redirect_uri=None, client_id=None):
        return authorization.ResolvedClient(
            client_id=client_id or "stored-1",
            client_secret="stored-secret",
            auth_method="client_secret_post",
            source=source,
        )

    wiring.monkeypatch.setattr(authorization, "resolve_client", _resolve_client)


async def test_no_pin_and_the_grants_server_no_longer_listed_requires_a_reconnect(
    wiring: _Wiring, fake: FakeOAuthServer
):
    mcp = _publish(fake, servers=("/b",))
    store = wiring.use(_record(grant=ConnectionGrant(issuer=f"{fake.base}/a")), make_discovered_descriptor(mcp_url=mcp))
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert _token_requests(fake) == []


async def test_a_pin_naming_another_server_than_the_grants_requires_a_reconnect(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake, servers=("/a", "/b"))
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a")),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/b"),
    )
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert _token_requests(fake) == []


@pytest.mark.parametrize("with_grant", [True, False])
async def test_a_pinned_issuer_the_server_does_not_list_is_a_failing_refresh_never_a_reconnect(
    wiring: _Wiring, fake: FakeOAuthServer, with_grant: bool
):
    mcp = _publish(fake, servers=("/a",))
    grant = ConnectionGrant(issuer=f"{fake.base}/b") if with_grant else None
    store = wiring.use(_record(grant=grant), make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/b"))
    with pytest.raises(ConnectorRefreshFailingError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.REFRESH_FAILING
    assert CID in wiring.cooldown
    assert _token_requests(fake) == []
    for request in fake.received:
        assert SECRET.encode() not in request.body


async def test_a_record_with_no_grant_under_a_pinned_descriptor_refreshes_with_the_env_client(
    wiring: _Wiring, fake: FakeOAuthServer
):
    mcp = _publish(fake, servers=("/b", "/a"))
    wiring.use(_record(grant=None), make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"))
    _rotating(fake)
    await resolve_managed_auth(CID, "remote", "files")
    (request,) = _token_requests(fake)
    assert request.path == "/a/token"
    assert request.form["client_id"] == "remote-env-id"


async def test_a_record_with_no_grant_under_a_descriptor_without_an_env_client_requires_a_reconnect(
    wiring: _Wiring, fake: FakeOAuthServer
):
    mcp = _publish(fake)
    store = wiring.use(_record(grant=None), make_discovered_descriptor(mcp_url=mcp))
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert _token_requests(fake) == []


def _declared_at(fake: FakeOAuthServer) -> ProviderDescriptor:
    discovered = make_discovered_descriptor()
    return discovered.model_copy(
        update={
            "oauth": OAuthEndpoints(authorize=f"{fake.base}/d/authorize", token=f"{fake.base}/d/token"),
            "client_id_env": "REMOTE_CLIENT_ID",
            "client_secret_env": "REMOTE_CLIENT_SECRET",
            "issuer": None,
        }
    )


async def test_an_env_client_grant_under_a_descriptor_declared_again_refreshes_the_declared_way(
    wiring: _Wiring, fake: FakeOAuthServer
):
    from tai42_kit.clients.base import shutdown_all_clients

    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", resource="https://r.example.com")), _declared_at(fake)
    )
    _rotating(fake, "/d/token")
    try:
        await resolve_managed_auth(CID, "remote", "files")
    finally:
        await shutdown_all_clients()
    (request,) = _token_requests(fake)
    assert request.path == "/d/token"
    assert "resource" not in request.form
    assert request.form["client_id"] == "remote-env-id"
    assert store.record().auth_health_state is AuthHealthState.HEALTHY


async def test_a_stored_client_grant_under_a_descriptor_declared_again_requires_a_reconnect(
    wiring: _Wiring, fake: FakeOAuthServer
):
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="stored-1")), _declared_at(fake)
    )
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert fake.received == []


async def test_a_stored_client_grant_under_a_descriptor_that_now_names_an_env_client_sends_nothing(
    wiring: _Wiring, fake: FakeOAuthServer
):
    mcp = _publish(fake)
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="stored-1")),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
    )
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert fake.received == []


async def test_a_refused_dynamic_client_requires_a_reconnect(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake)
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a", client_id="dyn-1")),
        make_discovered_descriptor(mcp_url=mcp),
    )
    monkey_client(wiring, source="dynamic")
    fake.route("POST", "/a/token", Reply(status=401, body={"error": "invalid_client"}))
    with pytest.raises(ConnectorReconnectRequiredError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED


async def test_an_env_secret_changed_between_two_attempts_is_used_by_the_second(
    wiring: _Wiring, fake: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch
):
    mcp = _publish(fake)
    wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a")),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
    )
    fake.route(
        "POST",
        "/a/token",
        [Reply(status=503), Reply(body={"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 60})],
    )
    real_sleep = asyncio.sleep

    async def _rotate_then_sleep(seconds):
        os.environ["REMOTE_CLIENT_SECRET"] = "rotated-secret"
        await real_sleep(0)

    monkeypatch.setattr(resolver_mod.asyncio, "sleep", _rotate_then_sleep)
    await resolve_managed_auth(CID, "remote", "files")
    first, second = _token_requests(fake)
    assert first.form["client_secret"] == SECRET
    assert second.form["client_secret"] == "rotated-secret"


async def test_a_discovery_failure_spends_the_transient_budget(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=None)  # no protected-resource document anywhere
    store = wiring.use(_record(grant=ConnectionGrant(issuer=f"{fake.base}/a")), make_discovered_descriptor(mcp_url=mcp))
    with pytest.raises(ConnectorRefreshFailingError):
        await resolve_managed_auth(CID, "remote", "files")
    assert store.record().auth_health_state is AuthHealthState.REFRESH_FAILING
    assert len(fake.requests_to("/mcp", "POST")) == resolver_mod.TRANSIENT_RETRY_BUDGET


# -- the section finishes ----------------------------------------------------------------


def _slow_rotation(wiring: _Wiring, fake: FakeOAuthServer, *, delay: float = 0.6) -> _Store:
    mcp = _publish(fake)
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a")),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
    )
    _rotating(fake, delay=delay)
    return store


def _assert_rotated(store: _Store) -> None:
    stored = store.record()
    assert stored.refresh_token is not None
    assert stored.refresh_token.get_secret_value() == "rt-rotated"
    assert stored.auth_health_state is AuthHealthState.HEALTHY


async def _warm(fake: FakeOAuthServer) -> None:
    # Fetch the documents first, so the timing below measures only the refresh section.
    await discovery.discover(f"{fake.base}/mcp", bound_issuer=f"{fake.base}/a")


async def test_a_caller_cut_by_wait_for_waits_for_the_write_back(wiring: _Wiring, fake: FakeOAuthServer):
    store = _slow_rotation(wiring, fake)
    await _warm(fake)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)
    assert time.monotonic() - started >= 0.55
    _assert_rotated(store)


async def test_the_same_through_a_throwaway_loop(wiring: _Wiring, fake: FakeOAuthServer):
    store = _slow_rotation(wiring, fake)
    await _warm(fake)

    async def _caller():
        return await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)

    with pytest.raises(TimeoutError):
        await asyncio.to_thread(run_blocking, _caller)
    _assert_rotated(store)


@types.coroutine
def _counting(coro, counter: list[int]):
    """Await ``coro``, counting every suspension (each one is a later wake-up of the caller)."""
    it = coro.__await__()
    value = None
    error: BaseException | None = None
    while True:
        try:
            yielded = it.send(value) if error is None else it.throw(error)
        except StopIteration as stop:
            return stop.value
        counter[0] += 1
        try:
            value = yield yielded
            error = None
        except BaseException as exc:  # a cancellation delivered to the caller
            value = None
            error = exc


async def test_a_caller_cut_by_an_anyio_scope_waits_without_spinning(wiring: _Wiring, fake: FakeOAuthServer):
    store = _slow_rotation(wiring, fake)
    await _warm(fake)
    suspensions = [0]
    started = time.monotonic()
    with anyio.move_on_after(0.2) as scope:
        await _counting(resolve_managed_auth(CID, "remote", "files"), suspensions)
    assert scope.cancelled_caught
    assert time.monotonic() - started >= 0.55
    _assert_rotated(store)
    assert suspensions[0] < 20


async def test_a_caller_cut_while_waiting_for_the_lock_stops_at_once_and_sends_nothing(
    wiring: _Wiring, fake: FakeOAuthServer
):
    _slow_rotation(wiring, fake)
    wiring.lock_gate = asyncio.Event()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)
    assert time.monotonic() - started < 0.5
    assert _token_requests(fake) == []


async def test_a_slow_write_back_completes_before_the_section_ends(wiring: _Wiring, fake: FakeOAuthServer):
    mcp = _publish(fake)
    store = wiring.use(
        _record(grant=ConnectionGrant(issuer=f"{fake.base}/a")),
        make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a"),
        write_delay=0.3,
    )
    _rotating(fake)
    await _warm(fake)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.1)
    assert len(store.database) == 1
    assert len(store.cache) == 1
    _assert_rotated(store)
