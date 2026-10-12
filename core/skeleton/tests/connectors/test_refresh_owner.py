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
    """A compare-and-set store; with ``hang`` its write sets ``entered`` and waits until ``release`` is set."""

    def __init__(self, record: ConnectionRecord, *, hang: bool = False) -> None:
        self.blob = crypto.encrypt(record.to_storage_json().encode(), connection_id=record.connection_id)
        self.cid = record.connection_id
        self.hang = hang
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes = 0

    def record(self) -> ConnectionRecord:
        return ConnectionRecord.model_validate_json(crypto.decrypt(self.blob, connection_id=self.cid))

    async def put(self, connection_id, blob, *, create_only=False, expected_blob=None, session_expires_at=None):
        if self.hang:
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        if expected_blob is not None and expected_blob != self.blob:
            return False
        self.blob = blob
        self.writes += 1
        return True


class _Wiring:
    def __init__(self) -> None:
        self.stores: dict[str, _Store] = {}
        self.lock_gates: dict[str, threading.Event] = {}
        self.lock_waiting: dict[str, threading.Event] = {}


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
        waiting = w.lock_waiting.get(connection_id)
        if waiting is not None:
            waiting.set()
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


async def _the_held_refresh_runs_on(store: _Store, loop: asyncio.AbstractEventLoop) -> None:
    """After the caller left: the refresh had produced nothing, reaches its write-back, and ends once released."""
    assert store.writes == 0
    assert await asyncio.to_thread(store.entered.wait, 10)

    async def _owned() -> asyncio.Task:
        return refresh_owner._tasks[CID]

    owned = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_owned(), loop))
    assert not owned.done()
    store.release.set()
    await _eventually_rotated(store)
    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(asyncio.wait([owned], timeout=10), loop))
    assert owned.done()
    assert not owned.cancelled()
    assert store.writes == 1


async def test_a_throwaway_loop_caller_leaves_at_its_budget_and_the_refresh_still_completes(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop
):
    # The refresh's write-back is held until released, so it cannot finish within the caller's budget.
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"), hang=True)
    _slow_token(fake, delay=0.0)
    await _warm(fake, owner_loop)

    async def _caller() -> float:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(resolve_managed_auth(CID, "remote", "files"), 0.2)
        return time.monotonic() - started

    # The outer bound only stops a caller that never leaves from hanging the test.
    elapsed = await asyncio.wait_for(asyncio.to_thread(run_blocking, _caller), 10)
    assert elapsed < 1.0
    await _the_held_refresh_runs_on(store, owner_loop)


@pytest.mark.parametrize("cancel", ["wait_for", "anyio"])
async def test_a_caller_on_the_owners_loop_leaves_at_its_budget(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop, cancel
):
    # The refresh's write-back is held until released, so it cannot finish within the caller's budget.
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"), hang=True)
    _slow_token(fake, delay=0.0)
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

    # The outer bound only stops a caller that never leaves from hanging the test.
    elapsed = await asyncio.wait_for(asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_caller(), owner_loop)), 10)
    assert elapsed < 1.0
    await _the_held_refresh_runs_on(store, owner_loop)


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
    # Each token request is answered only once both are in flight at the same time.
    both_in_flight = threading.Barrier(2, timeout=10)
    fake.route(
        "POST",
        "/a/token",
        [
            Reply(
                hold=both_in_flight.wait,
                body={"access_token": "at-1", "refresh_token": "rt-rotated", "expires_in": 3600},
            ),
            Reply(
                hold=both_in_flight.wait,
                body={"access_token": "at-2", "refresh_token": "rt-rotated", "expires_in": 3600},
            ),
        ],
    )
    first, second = await asyncio.gather(
        resolve_managed_auth(CID, "remote", "files"), resolve_managed_auth(CID2, "remote", "files")
    )
    assert first is not None
    assert second is not None
    assert {first.access_token, second.access_token} == {"at-1", "at-2"}
    assert not both_in_flight.broken
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
    store = wiring.stores[CID] = _Store(_expired(issuer=f"{fake.base}/a"), hang=True)
    _slow_token(fake, delay=0.0)
    monkeypatch.setattr(refresh_owner, "ACLOSE_TIMEOUT_SECONDS", 0.4)
    caller = asyncio.ensure_future(resolve_managed_auth(CID, "remote", "files"))
    # The owned refresh is inside its finishing section, waiting on the store's write.
    assert await asyncio.to_thread(store.entered.wait, 10)
    owned = refresh_owner._tasks[CID]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.connectors.runtime.refresh_owner"):
        await asyncio.wait_for(
            asyncio.wrap_future(asyncio.run_coroutine_threadsafe(refresh_owner.aclose(), owner_loop)), 10
        )
    assert 0.4 <= time.monotonic() - started < 1.5
    assert not owned.done()
    assert any(CID in record.message and record.levelno == logging.ERROR for record in caplog.records)
    store.release.set()
    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(asyncio.wait([owned], timeout=10), owner_loop))
    assert owned.cancelled()
    assert store.writes == 1


async def test_a_hand_over_landing_after_aclose_began_runs_inline_in_its_caller(
    owner_loop, caplog: pytest.LogCaptureFixture
):
    in_flight = threading.Event()

    async def _forever():
        in_flight.set()
        await asyncio.Event().wait()

    first = asyncio.ensure_future(refresh_owner.run(CID, _forever))
    assert await asyncio.to_thread(in_flight.wait, 10)
    release = threading.Event()
    owner_loop.call_soon_threadsafe(release.wait)  # holds the owner loop so aclose and the hand-over queue up
    closing = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(refresh_owner.aclose(), owner_loop))
    assert refresh_owner.is_live()
    loops: list[asyncio.AbstractEventLoop] = []

    async def _late():
        loops.append(asyncio.get_running_loop())
        return "late"

    late = asyncio.ensure_future(refresh_owner.run(CID2, _late))
    await asyncio.sleep(0)  # the late caller has handed its refresh over, queued behind aclose
    assert loops == []
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.connectors.runtime.refresh_owner"):
        release.set()
        await asyncio.wait_for(closing, 10)
        assert await asyncio.wait_for(late, 10) == "late"
    assert loops == [asyncio.get_running_loop()]
    assert refresh_owner._tasks == {}
    assert any(CID2 in record.message and record.levelno == logging.WARNING for record in caplog.records)
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_aclose_lets_a_section_write_back_and_cancels_a_lock_waiter(
    wiring: _Wiring, fake: FakeOAuthServer, owner_loop
):
    writing = wiring.stores[CID] = _Store(_expired(CID, issuer=f"{fake.base}/a"))
    waiting = wiring.stores[CID2] = _Store(_expired(CID2, issuer=f"{fake.base}/a"))
    wiring.lock_gates[CID2] = threading.Event()  # never opened: this one waits for the lock
    wiring.lock_waiting[CID2] = threading.Event()
    token_sent = threading.Event()
    answer_token = threading.Event()

    def _hold_token() -> None:
        token_sent.set()
        if not answer_token.wait(10):
            raise RuntimeError("the token answer was never released")

    fake.route(
        "POST",
        "/a/token",
        Reply(hold=_hold_token, body={"access_token": "at-new", "refresh_token": "rt-rotated", "expires_in": 3600}),
    )
    callers = [
        asyncio.ensure_future(resolve_managed_auth(CID, "remote", "files")),
        asyncio.ensure_future(resolve_managed_auth(CID2, "remote", "files")),
    ]
    # The first section is in flight, the second waits for its lock.
    assert await asyncio.to_thread(token_sent.wait, 10)
    assert await asyncio.to_thread(wiring.lock_waiting[CID2].wait, 10)
    closing = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(refresh_owner.aclose(), owner_loop))

    async def _after_aclose_began() -> None:
        return None

    # Queued behind aclose on the owner loop: once it runs, aclose has begun.
    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_after_aclose_began(), owner_loop))
    assert not refresh_owner.is_live()
    loops: list[asyncio.AbstractEventLoop] = []

    async def _start():
        loops.append(asyncio.get_running_loop())
        return "inline"

    assert await refresh_owner.run("other", _start) == "inline"
    assert loops == [asyncio.get_running_loop()]
    answer_token.set()
    await asyncio.wait_for(closing, 10)
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
