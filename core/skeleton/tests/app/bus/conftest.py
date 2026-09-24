"""Shared fixtures, builders, and pub/sub doubles for the worker-bus tests.

The bus is exercised over a real Redis pub/sub model — a shared ``fakeredis``
``FakeServer`` behind the bus's ``client_ctx`` seam. ``fakeredis`` genuinely models
pub/sub delivery, key TTL expiry (so missing-vs-departed is decided by a real
presence-key expiry, not a stub), the compare-token ``EVAL`` claim scripts, and
connection drops (``server.connected = False`` raises the same ``ConnectionError``
the real client does), so the reconnect, claim, and departed paths are driven, not
mocked away.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_contract.errors import ClientDisconnectedError

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app.bus import WorkerBus, WorkerIdentity, WorkerKind, WorkerState
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def wire_bus_client(monkeypatch: pytest.MonkeyPatch, server: fakeredis.FakeServer) -> None:
    """Route the bus's ``client_ctx`` to a fresh fake handle on the shared server."""

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs) -> AsyncIterator[aioredis.FakeRedis]:
        client = aioredis.FakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)


@pytest.fixture
def wire_pooled_bus_client(monkeypatch: pytest.MonkeyPatch, server: fakeredis.FakeServer) -> None:
    """Route the bus's ``client_ctx`` through a handle that models the REAL pooled
    wrapper, not the raw driver: a severed connection inside the body surfaces as
    ``ClientDisconnectedError``, because ``tai42_kit``'s ``client_ctx`` evicts the dead
    client and re-raises the disconnection wrapped in that type. This is the exact shape
    a live bus-Redis outage takes; the raw-``ConnectionError`` fixture bypasses the
    wrapper and so never exercises the wrapped transport path.
    """

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs) -> AsyncIterator[aioredis.FakeRedis]:
        client = aioredis.FakeRedis(server=server, decode_responses=True)
        try:
            yield client
        except RedisConnectionError as exc:
            raise ClientDisconnectedError(
                f"{type(client).__name__} disconnected and was removed from the cache. "
                f"Retry the operation to create a new client. (Original error: {exc})"
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)


def make_settings(namespace: str = "tai", **over: float) -> BusSettings:
    return BusSettings(
        redis=BusRedisSettings(redis_url="redis://fake"),
        namespace=namespace,
        ack_timeout=over.get("ack_timeout", 0.05),
        apply_timeout=over.get("apply_timeout", 0.3),
        heartbeat_ttl=over.get("heartbeat_ttl", 0.5),
    )


_pub_counter = itertools.count(1)


def make_bus(namespace: str = "tai", kind: WorkerKind = WorkerKind.serve, **over: float) -> WorkerBus:
    bus = WorkerBus(
        make_settings(namespace, **over),
        kind=kind,
        reconnect_backoff_initial=0.02,
        reconnect_backoff_max=0.05,
    )
    # In production a bus that publishes is always a claimed member (it subscribed at
    # boot). A pure-publisher test bus never subscribes, so give it a distinct pre-mint
    # identity to name itself on the wire — it can never collide with a claimed
    # ``{kind}-{n}`` slot, and a bus that DOES subscribe re-mints a real claim on entry.
    bus._identity = WorkerIdentity(name=f"{kind.value}-pub{next(_pub_counter)}", kind=kind, pid=1, generation=1)
    return bus


async def _spawn_subscriber(
    bus: WorkerBus,
    callback: Callable[[dict], Awaitable[object]],
) -> tuple[asyncio.Task[None], WorkerIdentity]:
    """Start a subscriber and wait until it is counted READY in the census (slot
    claimed, self-resync fired, presence written ready), so a subsequent whole-fleet
    publish is guaranteed to include it as expected."""
    ready = asyncio.Event()

    async def on_ready() -> None:
        ready.set()

    task = asyncio.create_task(bus.subscribe(callback, on_ready))
    await asyncio.wait_for(ready.wait(), timeout=2.0)

    async def _counted() -> bool:
        return any(row.name == bus.identity.name and row.state == WorkerState.ready for row in await bus.census())

    async with asyncio.timeout(2.0):
        while not await _counted():
            await asyncio.sleep(0.01)
    return task, bus.identity


async def _stop(*tasks: asyncio.Task[None]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _until(pred: Callable[[], bool]) -> None:
    while not pred():
        await asyncio.sleep(0.01)


async def _register_bare_presence(
    server: fakeredis.FakeServer,
    settings: BusSettings,
    name: str,
    kind: WorkerKind,
    ttl_ms: int | None,
    state: WorkerState = WorkerState.ready,
) -> None:
    """Write a presence key with no live subscriber behind it (models a worker that
    is counted by the census but will not reply)."""
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    now = "2026-01-01T00:00:00+00:00"
    value = json.dumps(
        {"kind": kind.value, "pid": 4, "generation": 1, "joined_at": now, "beat_at": now, "state": state.value}
    )
    if ttl_ms is None:
        await client.set(settings.presence_key(name), value)
    else:
        await client.set(settings.presence_key(name), value, px=ttl_ms)
    # The census reads names from the presence index, so a bare presence key is only
    # counted once its name is a member — exactly as a live subscriber's atomic write registers it.
    await client.sadd(settings.presence_index, name)
    await client.aclose()


async def _drop_presence(server: fakeredis.FakeServer, settings: BusSettings, name: str) -> None:
    """Delete a presence key out from under the census — the TTL fading (or a deliberate
    stop) during the publisher's local apply, as publish's own census would see it."""
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    await client.delete(settings.presence_key(name))
    await client.aclose()


def _make_nonmember_bus(kind: WorkerKind = WorkerKind.serve) -> WorkerBus:
    """A bus re-derived to a fork child's NON-MEMBER identity: {parent}/fork-{pid},
    generation 0, member False — it claims nothing and registers no presence."""
    bus = make_bus(kind=kind)
    parent = bus.identity
    bus._identity = WorkerIdentity(
        name=f"{parent.name}/fork-{os.getpid()}", kind=kind, pid=os.getpid(), generation=0, member=False
    )
    return bus


async def _noop_op(_op: dict) -> None:
    return None


class _FramePubsub:
    """A pubsub that yields a fixed queue of pre-built reply frames, then None — so
    the collect gates can be driven with crafted (name, generation, op_id) frames."""

    def __init__(self, frames: list) -> None:
        self._frames = list(frames)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> dict | None:
        if self._frames:
            return {"data": json.dumps(self._frames.pop(0))}
        return None


class _RaisingPubsub:
    """A pubsub whose poll raises a transport error, modelling a blip mid-collection."""

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> dict | None:
        raise RedisConnectionError("reply collection blip")


class _RecordingPipeline:
    """Wraps a real fakeredis pipeline and records every queued command name, so a test
    can assert the presence write and its index SADD ride ONE pipeline."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.commands: list[str] = []

    def __getattr__(self, name: str) -> object:
        real = getattr(self.inner, name)
        if name == "execute":
            return real
        if callable(real):

            def _record(*args: object, **kwargs: object) -> object:
                self.commands.append(name)
                return real(*args, **kwargs)

            return _record
        return real


class _RecordingRedis:
    """Wraps a fakeredis handle, recording every top-level attribute reached and every
    pipeline opened — so a test can prove the census never touches ``scan_iter`` /
    ``scan`` / ``keys`` and that the presence write queues one pipeline."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.touched: set[str] = set()
        self.pipelines: list[_RecordingPipeline] = []

    def pipeline(self, *args: object, **kwargs: object) -> _RecordingPipeline:
        self.touched.add("pipeline")
        rec = _RecordingPipeline(self.inner.pipeline(*args, **kwargs))
        self.pipelines.append(rec)
        return rec

    def __getattr__(self, name: str) -> object:
        self.touched.add(name)
        return getattr(self.inner, name)


@pytest.fixture
def recording_bus_client(monkeypatch: pytest.MonkeyPatch, server: fakeredis.FakeServer) -> list[_RecordingRedis]:
    """Route the bus's ``client_ctx`` to recording handles and hand the test the list of
    handles opened, so it can assert which commands the census did and did not issue."""
    clients: list[_RecordingRedis] = []

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs) -> AsyncIterator[_RecordingRedis]:
        inner = aioredis.FakeRedis(server=server, decode_responses=True)
        rec = _RecordingRedis(inner)
        clients.append(rec)
        try:
            yield rec
        finally:
            await inner.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)
    return clients
