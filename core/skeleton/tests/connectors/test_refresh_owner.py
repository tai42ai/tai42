"""The refresh owner: a credential refresh runs on the serving loop, not in whoever asked.

A real second event loop running in its own thread stands for the serving loop; the token endpoint is
a loopback protocol fixture and the store an in-memory compare-and-set fake.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.app.lifecycle.off_loop import run_blocking
from tai42_skeleton.connectors.oauth import crypto, discovery
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.runtime.resolver import ConnectorReconnectRequiredError, resolve_managed_auth

from .conftest import CID, CID2, make_discovered_descriptor
from .oauth_fakes import FakeOAuthServer, Reply, serve


class _Store:
    def __init__(self, record: ConnectionRecord, *, hang: bool = False) -> None:
        self.blob = crypto.encrypt(record.to_storage_json().encode(), connection_id=record.connection_id)
        self.cid = record.connection_id
        self.hang = hang
        self.writes = 0

    def record(self) -> ConnectionRecord:
        return ConnectionRecord.model_validate_json(crypto.decrypt(self.blob, connection_id=self.cid))

    async def put(self, connection_id, blob, *, create_only=False, expected_blob=None, session_expires_at=None):
        if self.hang:
            await asyncio.Event().wait()
        if expected_blob is not None and expected_blob != self.blob:
            return False
        self.blob = blob
        self.writes += 1
        return True


class _Wiring:
    def __init__(self) -> None:
        self.stores: dict[str, _Store] = {}
        self.lock_gates: dict[str, threading.Event] = {}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    monkeypatch.setenv("REMOTE_CLIENT_ID", "remote-env-id")
    monkeypatch.setenv("REMOTE_CLIENT_SECRET", "remote-env-secret")
    reset_all_settings()
    discovery.reset_discovery_cache()
    refresh_owner.reset()
    yield
    discovery.reset_discovery_cache()
    refresh_owner.reset()


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch, fake: FakeOAuthServer) -> _Wiring:
    w = _Wiring()
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}/a"]})
    fake.authorization_server("/a")
    descriptor = make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a")

    async def _load(connection_id):
        store = w.stores[connection_id]
        return store.record(), store.blob

    @asynccontextmanager
    async def _lock(connection_id):
        gate = w.lock_gates.get(connection_id)
        while gate is not None and not gate.is_set():
            await asyncio.sleep(0.01)
        yield

    async def _false(cid, *args):
        return False

    async def _none(cid):
        return None

    async def _claim(cid, seconds):
        return False

    monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
    monkeypatch.setattr(resolver_mod, "connection_lock", _lock)
    monkeypatch.setattr(resolver_mod, "token_store", lambda: _StoreRouter(w))
    monkeypatch.setattr(resolver_mod, "get_provider", lambda pid: descriptor)
    monkeypatch.setattr(resolver_mod, "refresh_cooldown_active", _false)
    monkeypatch.setattr(resolver_mod, "open_refresh_cooldown", _none)
    monkeypatch.setattr(resolver_mod, "clear_refresh_cooldown", _none)
    monkeypatch.setattr(resolver_mod, "claim_keep_alive", _claim)
    return w


class _StoreRouter:
    def __init__(self, wiring: _Wiring) -> None:
        self._wiring = wiring

    async def put(self, connection_id, blob, **kwargs):
        return await self._wiring.stores[connection_id].put(connection_id, blob, **kwargs)


def _expired(cid: str = CID, *, issuer: str) -> ConnectionRecord:
    now = datetime.now(UTC)
    return ConnectionRecord(
        connection_id=cid,
        provider_id="remote",
        kind="oauth",
        alias=f"team{cid[0]}",
        account_identity="a@example.com",
        enabled_sub_services=["files"],
        access_token=SecretStr("at-old"),
        refresh_token=SecretStr("rt-old"),
        access_token_expires_at=now - timedelta(seconds=5),
        auth_health_state=AuthHealthState.HEALTHY,
        grant=ConnectionGrant(issuer=issuer),
        created_at=now,
    )


def _slow_token(fake: FakeOAuthServer, *, delay: float = 0.6, body: dict | None = None, status: int = 200) -> None:
    fake.route(
        "POST",
        "/a/token",
        Reply(
            status=status,
            delay=delay,
            body=body or {"access_token": "at-new", "refresh_token": "rt-rotated", "expires_in": 3600},
        ),
    )


@pytest.fixture
def owner_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A long-lived loop in its own thread, bound as the refresh owner (the serving loop's stand-in)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _bind() -> None:
        refresh_owner.bind_to_running_loop()

    asyncio.run_coroutine_threadsafe(_bind(), loop).result(5)
    try:
        yield loop
    finally:
        from tai42_kit.clients.base import shutdown_all_clients

        async def _close() -> None:
            await refresh_owner.aclose()
            await shutdown_all_clients()

        asyncio.run_coroutine_threadsafe(_close(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


async def _eventually_rotated(store: _Store, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = store.record()
        if stored.refresh_token is not None and stored.refresh_token.get_secret_value() == "rt-rotated":
            assert stored.auth_health_state is AuthHealthState.HEALTHY
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the refresh did not complete")


async def _warm(fake: FakeOAuthServer, loop: asyncio.AbstractEventLoop | None = None) -> None:
    coro = discovery.discover(f"{fake.base}/mcp", bound_issuer=f"{fake.base}/a")
    if loop is None:
        await coro
    else:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


# -- leaving at the budget ----------------------------------------------------------------


async def test_a_throwaway_loop_caller_leaves_at_its_budget_and_the_refresh_still_completes(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop
):
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"))
    _slow_token(fake)
    await _warm(fake, owner_loop)

    async def _caller():
        return await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(run_blocking, _caller)
    assert time.monotonic() - started < 0.5
    await _eventually_rotated(store)


@pytest.mark.parametrize("cancel", ["wait_for", "anyio"])
async def test_a_caller_on_the_owners_loop_leaves_at_its_budget(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop, cancel
):
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"))
    _slow_token(fake)
    await _warm(fake, owner_loop)

    async def _caller() -> float:
        started = time.monotonic()
        if cancel == "wait_for":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)
        else:
            with anyio.move_on_after(0.2) as scope:
                await resolve_managed_auth(CID, "remote", "files")
            assert scope.cancelled_caught
        return time.monotonic() - started

    elapsed = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_caller(), owner_loop))
    assert elapsed < 0.5
    await _eventually_rotated(store)


# -- one refresh per connection ---------------------------------------------------------


async def test_two_callers_for_one_connection_cause_one_token_request(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop
):
    wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"))
    _slow_token(fake, delay=0.3)
    first, second = await asyncio.gather(
        resolve_managed_auth(CID, "remote", "files"), resolve_managed_auth(CID, "remote", "files")
    )
    assert first is not None
    assert second is not None
    assert first.access_token == second.access_token == "at-new"
    assert len(fake.requests_to("/a/token")) == 1


async def test_callers_for_two_connections_run_concurrently(wiring: _Wiring, fake: FakeOAuthServer, owner_loop):
    wiring.stores[CID] = _Store(_expired(CID, issuer=f"{fake.base}/a"))
    wiring.stores[CID2] = _Store(_expired(CID2, issuer=f"{fake.base}/a"))
    _slow_token(fake, delay=0.5)
    fake.route(
        "POST",
        "/a/token",
        [
            Reply(delay=0.5, body={"access_token": "at-1", "refresh_token": "rt-rotated", "expires_in": 3600}),
            Reply(delay=0.5, body={"access_token": "at-2", "refresh_token": "rt-rotated", "expires_in": 3600}),
        ],
    )
    await _warm(fake, owner_loop)
    started = time.monotonic()
    await asyncio.gather(resolve_managed_auth(CID, "remote", "files"), resolve_managed_auth(CID2, "remote", "files"))
    assert time.monotonic() - started < 0.95
    assert len(fake.requests_to("/a/token")) == 2


async def test_a_typed_failure_reaches_every_waiting_caller(wiring: _Wiring, fake: FakeOAuthServer, owner_loop):
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"))
    _slow_token(fake, delay=0.3, status=400, body={"error": "invalid_grant"})
    results = await asyncio.gather(
        resolve_managed_auth(CID, "remote", "files"),
        resolve_managed_auth(CID, "remote", "files"),
        return_exceptions=True,
    )
    assert all(isinstance(result, ConnectorReconnectRequiredError) for result in results)
    assert store.record().auth_health_state is AuthHealthState.RECONNECT_REQUIRED
    assert len(fake.requests_to("/a/token")) == 1


# -- no live owner -------------------------------------------------------------------------


async def test_not_bound_runs_inline():
    loops: list[asyncio.AbstractEventLoop] = []

    async def _start():
        loops.append(asyncio.get_running_loop())
        return "done"

    assert not refresh_owner.is_live()
    assert await refresh_owner.run(CID, _start) == "done"
    assert loops == [asyncio.get_running_loop()]


async def test_an_owner_bound_in_another_process_is_not_live(owner_loop, monkeypatch: pytest.MonkeyPatch):
    assert refresh_owner.is_live()
    monkeypatch.setattr(refresh_owner, "_pid", -1)
    assert not refresh_owner.is_live()
    loops: list[asyncio.AbstractEventLoop] = []

    async def _start():
        loops.append(asyncio.get_running_loop())
        return "inline"

    assert await refresh_owner.run(CID, _start) == "inline"
    assert loops == [asyncio.get_running_loop()]
    assert loops[0] is not owner_loop


async def test_a_task_whose_only_waiter_left_logs_its_failure(owner_loop, caplog: pytest.LogCaptureFixture):
    async def _start():
        await asyncio.sleep(0.2)
        raise RuntimeError("the slow path broke")

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.connectors.runtime.refresh_owner"):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(refresh_owner.run(CID, _start), 0.05)
        await asyncio.sleep(0.4)
    assert any("owned refresh of" in record.message and record.levelno == logging.ERROR for record in caplog.records)


# -- shutdown -----------------------------------------------------------------------------


async def test_aclose_with_a_store_that_never_answers_returns_at_its_bound(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop, monkeypatch: pytest.MonkeyPatch, caplog
):
    wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"), hang=True)
    _slow_token(fake, delay=0.0)
    monkeypatch.setattr(refresh_owner, "ACLOSE_TIMEOUT_SECONDS", 0.4)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.3)
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.connectors.runtime.refresh_owner"):
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(refresh_owner.aclose(), owner_loop))
    assert time.monotonic() - started < 1.5
    assert any(CID in record.message and record.levelno == logging.ERROR for record in caplog.records)


async def test_aclose_lets_a_section_write_back_and_cancels_a_lock_waiter(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop
):
    writing = wiring.stores[CID] = _Store(_expired(CID, issuer=f"{fake.base}/a"))
    waiting = wiring.stores[CID2] = _Store(_expired(CID2, issuer=f"{fake.base}/a"))
    wiring.lock_gates[CID2] = threading.Event()  # never opened: this one waits for the lock
    _slow_token(fake, delay=0.5)
    await _warm(fake, owner_loop)
    callers = [
        asyncio.ensure_future(resolve_managed_auth(CID, "remote", "files")),
        asyncio.ensure_future(resolve_managed_auth(CID2, "remote", "files")),
    ]
    await asyncio.sleep(0.2)  # the first section is in flight, the second waits for its lock
    closing = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(refresh_owner.aclose(), owner_loop))
    await asyncio.sleep(0.05)
    assert not refresh_owner.is_live()
    loops: list[asyncio.AbstractEventLoop] = []

    async def _start():
        loops.append(asyncio.get_running_loop())
        return "inline"

    assert await refresh_owner.run("other", _start) == "inline"
    assert loops == [asyncio.get_running_loop()]
    await closing
    results = await asyncio.gather(*callers, return_exceptions=True)
    assert all(isinstance(result, BaseException) for result in results)
    await _eventually_rotated(writing)
    assert waiting.writes == 0
    assert len(fake.requests_to("/a/token")) == 1


async def test_a_blocked_owner_loop_holds_no_caller_past_its_budget(wiring: _Wiring, fake: FakeOAuthServer, owner_loop):
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"))
    _slow_token(fake, delay=0.1)
    await _warm(fake, owner_loop)
    release = threading.Event()
    owner_loop.call_soon_threadsafe(release.wait)  # the shape of a configuration reload's build

    async def _caller():
        return await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(run_blocking, _caller)
    assert time.monotonic() - started < 0.5
    assert fake.requests_to("/a/token") == []
    release.set()
    await _eventually_rotated(store)
