"""One process life on the census: the slot claim, the presence heartbeat, the presence row's
writes, the departure announce, and the life's end (``leave_census``) after the subscription's."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ReadOnlyError, ResponseError
from tai42_contract.backend import PROCESS_PRELUDE_ENV

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app.bus import OpOutcome, WorkerBus, WorkerKind, WorkerState, _PresenceValue
from tai42_skeleton.app.bus.life import _Heartbeat
from tai42_skeleton.app.bus.models import heartbeat_interval
from tai42_skeleton.app.bus.ops import ReloadConfig
from tai42_skeleton.config.deployment_env import published_env_readers

from ..._fakes.kit_redis import KitFakeRedis
from .conftest import (
    _make_nonmember_bus,
    _noop_op,
    _RecordingRedis,
    _spawn_subscriber,
    _start,
    _stop,
    _until,
    make_bus,
)

_RELOAD = ReloadConfig(op="reload_config")


def _live_heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("tai-worker-bus-heartbeat-")]


async def _end_subscription(task: asyncio.Task[None]) -> None:
    """The subscription's deliberate end alone: cancel its task and await it."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# -- slot claim / generation / ownership (compare-token) ----------------------


async def test_two_serve_workers_claim_distinct_ordinals(wire_bus_client: None) -> None:
    bus_a = make_bus(kind=WorkerKind.serve)
    bus_b = make_bus(kind=WorkerKind.serve)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    t1, id_a = await _spawn_subscriber(bus_a, noop)
    t2, id_b = await _spawn_subscriber(bus_b, noop)
    try:
        assert {id_a.name, id_b.name} == {"serve-1", "serve-2"}
    finally:
        await _stop(t1, t2)


async def test_reclaim_after_release_reuses_name_at_next_generation(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        first = await bus._claim_slot(client)
        assert first.name == "serve-1"
        assert first.generation == 1
        assert await bus._release_claim(client, "serve-1") is True

        second = await bus._claim_slot(client)
        # Same name reused (now free), monotonic generation per name.
        assert second.name == "serve-1"
        assert second.generation == 2

        assert await bus._release_claim(client, "serve-1") is True
        third = await bus._claim_slot(client)
        assert third.name == "serve-1"
        assert third.generation == 3
    finally:
        await client.aclose()


async def test_renew_and_release_are_ownership_checked(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._claim_slot(client)
        # A held claim renews.
        assert await bus._renew_claim(client, "serve-1") is True
        # A foreign token now owns the slot: renew MISSES atomically (never clobbers),
        # and release no-ops (no DEL of another holder's claim).
        await client.set(bus._settings.slot_key("serve-1"), "foreign-token", px=15000)
        assert await bus._renew_claim(client, "serve-1") is False
        assert await bus._release_claim(client, "serve-1") is False
        # The foreign token is intact — renew never overwrote it.
        assert await client.get(bus._settings.slot_key("serve-1")) == "foreign-token"
    finally:
        await client.aclose()


async def test_establish_identity_keeps_held_claim_but_remints_a_lost_one(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        first = bus.identity
        assert first.name == "serve-1"

        # A reconnect that still holds its claim keeps the SAME name+generation.
        await bus._establish_identity(client)
        assert bus.identity.name == first.name
        assert bus.identity.generation == first.generation

        # The slot is stolen: the next (re)establish renews-misses and re-mints a NEW
        # life — the lowest free ordinal (serve-1 is taken by the thief), fresh gen.
        await client.set(bus._settings.slot_key("serve-1"), "foreign-token", px=15000)
        await bus._establish_identity(client)
        assert bus.identity.name == "serve-2"
        assert bus.identity.generation == 1
    finally:
        await client.aclose()


# -- the heartbeat lives off the subscription loop ------------------------------


async def test_claim_survives_a_subscription_loop_stall(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # An op callback that blocks the subscription loop's thread synchronously for longer
    # than the claim TTL (a config reload's rebuild) keeps the claim and the presence row:
    # the heartbeat renews from its own thread, so the worker keeps its life and resyncs once.
    ttl = 0.6
    worker = make_bus(heartbeat_ttl=ttl)
    publisher = make_bus(heartbeat_ttl=ttl, apply_timeout=5.0)
    readies: list[str] = []

    async def stall(_op: dict, *, deadline: float) -> None:
        time.sleep(ttl * 2.5)

    async def on_ready() -> None:
        readies.append("ready")

    task = _start(worker, stall, on_ready)
    probe: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(_until(lambda: len(readies) >= 1), timeout=3.0)
            first = worker.identity
            assert (first.name, first.generation) == ("serve-1", 1)
            result = await publisher.publish(_RELOAD, targets=[first.name], local=None)
            # The next beat after the stall must still find the claim held.
            await asyncio.sleep(ttl)

        assert [(r.name, r.outcome) for r in result.results] == [(first.name, OpOutcome.applied)]
        assert (worker.identity.name, worker.identity.generation) == ("serve-1", 1)
        assert readies == ["ready"]
        messages = [r.getMessage() for r in caplog.records]
        assert not any("presence heartbeat for" in m or "re-minting" in m for m in messages), messages
        assert not any(r.exc_info and isinstance(r.exc_info[1], bus_module.SlotLostError) for r in caplog.records)
        assert await probe.get(worker._settings.slot_key("serve-1")) == worker._claim_token
        assert await probe.pttl(worker._settings.slot_key("serve-1")) > 0
        presence_pttl = await probe.pttl(worker._settings.presence_key("serve-1"))
        assert bus_module.presence_fresh(presence_pttl, ttl), presence_pttl
    finally:
        await probe.aclose()
        await _stop(task)


async def test_heartbeat_beats_off_the_subscription_loop(
    server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a process prelude the heartbeat runs on a dedicated named thread with its own
    # event loop and its own fresh (non-pooled) connection, never on the loop the subscription
    # serves from.
    monkeypatch.delenv(PROCESS_PRELUDE_ENV, raising=False)
    opened: list[tuple[str, bool]] = []

    @asynccontextmanager
    async def spy(client_cls, settings=None, *, fresh: bool = False, **kw) -> AsyncIterator[KitFakeRedis]:
        opened.append((threading.current_thread().name, fresh))
        client = KitFakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", spy)
    worker = make_bus(heartbeat_ttl=0.3)
    beats: list[tuple[asyncio.AbstractEventLoop, str]] = []
    real_heartbeat = worker._heartbeat_loop

    async def recording_heartbeat(r: object, presence_key: str) -> None:
        beats.append((asyncio.get_running_loop(), threading.current_thread().name))
        await real_heartbeat(r, presence_key)

    monkeypatch.setattr(worker, "_heartbeat_loop", recording_heartbeat)
    task = _start(worker, _noop_op)
    try:
        await asyncio.wait_for(_until(lambda: bool(beats)), timeout=3.0)
        loop, thread_name = beats[0]
        assert loop is not asyncio.get_running_loop()
        assert thread_name == "tai-worker-bus-heartbeat-serve-1"
        assert (thread_name, True) in opened, opened
    finally:
        await _stop(task)


async def test_under_a_process_prelude_the_heartbeat_beats_as_a_task_of_the_subscription_loop(
    server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A process run under a process prelude has no OS thread of its own to give the heartbeat:
    # it beats as a task of the loop the subscription serves from, still on a fresh connection.
    monkeypatch.setenv(PROCESS_PRELUDE_ENV, "gevent")
    opened: list[bool] = []

    @asynccontextmanager
    async def spy(client_cls, settings=None, *, fresh: bool = False, **kw) -> AsyncIterator[KitFakeRedis]:
        opened.append(fresh)
        client = KitFakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", spy)
    worker = make_bus(heartbeat_ttl=0.3)
    beats: list[tuple[asyncio.AbstractEventLoop, threading.Thread]] = []
    real_heartbeat = worker._heartbeat_loop

    async def recording_heartbeat(r: object, presence_key: str) -> None:
        beats.append((asyncio.get_running_loop(), threading.current_thread()))
        await real_heartbeat(r, presence_key)

    monkeypatch.setattr(worker, "_heartbeat_loop", recording_heartbeat)
    task = _start(worker, _noop_op)
    try:
        await asyncio.wait_for(_until(lambda: bool(beats)), timeout=3.0)
        assert beats[0] == (asyncio.get_running_loop(), threading.current_thread())
        assert _live_heartbeat_threads() == []
        assert True in opened
    finally:
        await _stop(task)


async def test_a_heartbeat_stopped_before_its_body_starts_never_runs_it() -> None:
    ran: list[str] = []

    async def main() -> None:
        ran.append("body")

    heartbeat = _Heartbeat("tai-worker-bus-heartbeat-test", main)
    heartbeat.request_stop()
    heartbeat.start()
    await asyncio.wait_for(asyncio.wrap_future(heartbeat.done), timeout=2.0)
    heartbeat.join(2.0)
    assert ran == []
    assert heartbeat.done.exception() is None


async def test_a_heartbeat_that_returns_cleanly_is_a_loud_failure(caplog: pytest.LogCaptureFixture) -> None:
    # The beat loop is unbounded, so a clean return is never an expected end: the
    # supervision turns it into a RuntimeError logged at ERROR.
    bus = make_bus()

    async def main() -> None:
        return None

    heartbeat = _Heartbeat("tai-worker-bus-heartbeat-test", main)
    heartbeat.start()
    await asyncio.wait_for(asyncio.wrap_future(heartbeat.done), timeout=2.0)
    with caplog.at_level(logging.ERROR):
        failure = bus._heartbeat_failure(heartbeat, "serve-1")
    assert isinstance(failure, RuntimeError)
    assert "returned without an error" in str(failure)
    assert any("presence heartbeat for serve-1 stopped" in r.getMessage() for r in caplog.records)
    heartbeat.join(2.0)


async def test_a_heartbeat_failing_while_its_stop_is_pending_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    # A heartbeat that dies on its own after the supervision last looked is not lost:
    # the teardown's join logs its exception at ERROR.
    bus = make_bus()

    async def main() -> None:
        raise RedisConnectionError("heartbeat connection dropped")

    heartbeat = _Heartbeat("tai-worker-bus-heartbeat-test", main)
    bus._heartbeat = heartbeat
    heartbeat.start()
    await asyncio.wait_for(_until(heartbeat.done.done), timeout=2.0)
    with caplog.at_level(logging.ERROR):
        await bus._stop_heartbeat()
    assert heartbeat.thread is not None
    assert not heartbeat.thread.is_alive()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "terminated abnormally" in errors[0].getMessage()
    assert isinstance(errors[0].exc_info and errors[0].exc_info[1], RedisConnectionError)


async def test_a_heartbeat_thread_that_overruns_its_stop_is_logged_and_left(caplog: pytest.LogCaptureFixture) -> None:
    # The join is bounded by one claim TTL: a body that cannot be cancelled (it blocks its
    # own thread) is reported at ERROR and left to its daemon status so teardown runs.
    bus = make_bus(heartbeat_ttl=0.1)
    release = threading.Event()

    async def main() -> None:
        release.wait(5.0)

    heartbeat = _Heartbeat("tai-worker-bus-heartbeat-test", main)
    bus._heartbeat = heartbeat
    heartbeat.start()
    await asyncio.wait_for(_until(lambda: heartbeat._task is not None), timeout=2.0)
    try:
        with caplog.at_level(logging.ERROR):
            await bus._stop_heartbeat()
        assert any("did not stop within 0.10s" in r.getMessage() for r in caplog.records)
        assert bus._heartbeat is None
        assert heartbeat.thread is not None
        assert heartbeat.thread.is_alive()
    finally:
        release.set()
        heartbeat.join(5.0)


# -- presence writes: atomic row + index, renew-gated -----------------------------


async def test_set_presence_writes_key_and_index_member_in_one_pipeline(server) -> None:
    bus = make_bus(kind=WorkerKind.serve)
    inner: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    recording = _RecordingRedis(inner)
    presence = _PresenceValue(
        kind=WorkerKind.serve,
        pid=1,
        generation=1,
        joined_at="2026-01-01T00:00:00+00:00",
        beat_at="2026-01-01T00:00:00+00:00",
        state=WorkerState.ready,
        deployment_keys=(),
        env_readers="",
    )
    key = bus._settings.presence_key(bus.identity.name)

    await bus._set_presence(recording, key, presence)

    # The reader body and its life, the row and the index SADD ride ONE pipeline, in that order —
    # a presence key never exists without its index member or without the body its digest names.
    assert len(recording.pipelines) == 1
    assert recording.pipelines[0].commands == ["set", "pexpire", "set", "sadd"]
    assert bus.identity.name in await inner.smembers(bus._settings.presence_index)
    row = json.loads(await inner.get(key))
    readers = published_env_readers()
    assert row["env_readers"] == readers.digest
    body_key = bus._settings.env_readers_key(readers.digest)
    assert await inner.get(body_key) == readers.body
    row_ttl_ms = int(bus._settings.heartbeat_ttl * 1000)
    # A lone write leaves the body twice the row's TTL to live.
    assert row_ttl_ms < await inner.pttl(body_key) <= 2 * row_ttl_ms
    assert 0 < await inner.pttl(key) <= row_ttl_ms
    await inner.aclose()


async def test_a_beat_with_a_short_ttl_never_cuts_a_shared_reader_body_below_a_longer_row(server) -> None:
    # Two workers with one reader set and different heartbeat TTLs share one body key: the
    # short-TTL worker's beat after the long-TTL worker's leaves the body outliving the long row.
    long_bus = make_bus(kind=WorkerKind.serve, heartbeat_ttl=45.0)
    short_bus = make_bus(kind=WorkerKind.serve, heartbeat_ttl=1.0)
    inner: Any = aioredis.FakeRedis(server=server, decode_responses=True)

    def presence() -> _PresenceValue:
        return _PresenceValue(
            kind=WorkerKind.serve,
            pid=1,
            generation=1,
            joined_at="2026-01-01T00:00:00+00:00",
            beat_at="2026-01-01T00:00:00+00:00",
            state=WorkerState.ready,
            deployment_keys=(),
            env_readers="",
        )

    long_key = long_bus._settings.presence_key(long_bus.identity.name)
    await long_bus._set_presence(inner, long_key, presence())
    await short_bus._set_presence(inner, short_bus._settings.presence_key(short_bus.identity.name), presence())

    body_key = long_bus._settings.env_readers_key(published_env_readers().digest)
    assert await inner.pttl(body_key) > 45_000 >= await inner.pttl(long_key)
    await inner.aclose()


async def test_presence_writes_are_skipped_on_a_lost_claim(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A renew-gated presence write (mark_recycling, the last_op stamp) skips silently
    # when the claim is lost — an ex-owner in the loss-lag never regresses the census
    # under its stale name; the re-mint rides the heartbeat's SlotLostError instead.
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        name = bus.identity.name
        now = "2026-01-01T00:00:00+00:00"
        bus._presence = _PresenceValue(
            kind=WorkerKind.serve,
            pid=1,
            generation=bus.identity.generation,
            joined_at=now,
            beat_at=now,
            state=WorkerState.ready,
            deployment_keys=(),
            env_readers="",
        )
        # Steal the claim so every renew misses.
        await client.set(bus._settings.slot_key(name), "foreign-token", px=30000)

        await bus.mark_recycling()
        assert await client.get(bus._settings.presence_key(name)) is None  # the write was skipped

        await bus._stamp_last_op(client, "reload_config", "applied")
        assert bus._presence.last_op is None  # not stamped — the write was skipped
    finally:
        await client.aclose()


# -- the departure announce: recycling with the declared drain -----------------


async def test_mark_recycling_writes_the_recycling_state(wire_bus_client: None) -> None:
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        await bus.mark_recycling()
        rows = {r.name: r for r in await bus.census()}
        assert rows[identity.name].state == WorkerState.recycling
    finally:
        await _stop(task)


async def test_mark_recycling_announces_the_drain_this_process_declared(wire_bus_client: None) -> None:
    bus = make_bus(heartbeat_ttl=5.0, drain=630.0)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        # A worker not leaving announces no drain.
        assert {r.name: r for r in await bus.census()}[identity.name].drain_seconds is None
        await bus.mark_recycling()
        row = {r.name: r for r in await bus.census()}[identity.name]
        assert (row.state, row.drain_seconds) == (WorkerState.recycling, 630.0)
    finally:
        await _stop(task)


async def test_mark_recycling_announces_no_drain_when_this_process_declared_none(wire_bus_client: None) -> None:
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        await bus.mark_recycling()
        row = {r.name: r for r in await bus.census()}[identity.name]
        assert (row.state, row.drain_seconds) == (WorkerState.recycling, None)
    finally:
        await _stop(task)


async def test_mark_recycling_is_a_noop_without_presence() -> None:
    # An unsubscribed bus holds no presence state, so marking recycling is a no-op
    # (nothing to write) and never touches redis.
    bus = make_bus()
    await bus.mark_recycling()  # must not raise


async def test_mark_recycling_swallows_a_transport_blip(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A transport blip on the recycling-state write is logged and not raised, so the stop
    # it begins goes on; the heartbeat's next beat carries the leaving to the row. The renew
    # succeeds; the presence SET raises a transport error.
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, _ = await _spawn_subscriber(bus, noop)
    try:

        async def blip(*_a: object, **_k: object) -> None:
            raise RedisConnectionError("recycling-state write lost its connection")

        monkeypatch.setattr(bus, "_set_presence", blip)
        with caplog.at_level(logging.WARNING):
            await bus.mark_recycling()  # must NOT raise

        assert any("recycling-state write" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    finally:
        await _stop(task)


async def test_a_leaving_whose_announce_hit_a_transport_blip_reaches_the_row_on_the_next_beat(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ttl = 0.6
    bus = make_bus(heartbeat_ttl=ttl, drain=630.0)
    task, identity = await _spawn_subscriber(bus, _noop_op)
    real_renew = bus._renew_claim

    async def a_renew_whose_connection_drops_on_the_announce(r: Any, name: str) -> bool:
        if threading.current_thread() is threading.main_thread():
            raise RedisConnectionError("the announce lost its connection")
        return await real_renew(r, name)

    monkeypatch.setattr(bus, "_renew_claim", a_renew_whose_connection_drops_on_the_announce)
    try:
        await bus.mark_recycling()
        async with asyncio.timeout(ttl * 3):
            while True:
                row = {r.name: r for r in await bus.census()}[identity.name]
                if row.state is WorkerState.recycling:
                    break
                await asyncio.sleep(0.02)
        assert row.drain_seconds == 630.0
    finally:
        await _stop(task)


async def test_a_second_departure_announce_writes_nothing_and_keeps_the_first_drain(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = make_bus(heartbeat_ttl=5.0)
    drains = [630.0]
    monkeypatch.setattr(bus, "_exit_drain", lambda: drains[0])
    task, identity = await _spawn_subscriber(bus, _noop_op)
    announce_writes: list[str] = []
    real_set = bus._set_presence

    async def recording_set(r: Any, presence_key: str, presence: _PresenceValue) -> None:
        if threading.current_thread() is threading.main_thread():
            announce_writes.append(presence.state.value)
        await real_set(r, presence_key, presence)

    try:
        await bus.mark_recycling()
        drains[0] = 5.0
        monkeypatch.setattr(bus, "_set_presence", recording_set)
        await bus.mark_recycling()

        assert announce_writes == []
        row = {r.name: r for r in await bus.census()}[identity.name]
        assert (row.state, row.drain_seconds) == (WorkerState.recycling, 630.0)
    finally:
        await _stop(task)


# -- the life's end: after the subscription and the shutdown handlers ------------


async def test_the_life_outlives_the_subscription_until_it_leaves_the_census(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ttl = 0.6
    bus = make_bus(heartbeat_ttl=ttl)
    task, ident = await _spawn_subscriber(bus, _noop_op)
    settings = bus._settings
    presence_key = settings.presence_key(ident.name)
    heartbeat_alive_at_release: list[bool] = []
    real_release = bus._release_claim

    async def recording_release(r: Any, name: str) -> bool:
        heartbeat_alive_at_release.append(bool(_live_heartbeat_threads()))
        return await real_release(r, name)

    monkeypatch.setattr(bus, "_release_claim", recording_release)
    probe: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await _end_subscription(task)

        # The subscription's deliberate end leaves the life in place: the claim, the row, its
        # index member, and one heartbeat still beating.
        assert await probe.get(settings.slot_key(ident.name)) == bus._claim_token
        assert ident.name in await probe.smembers(settings.presence_index)
        before = json.loads(await probe.get(presence_key))["beat_at"]
        assert len(_live_heartbeat_threads()) == 1
        # Longer than one TTL: an unrefreshed row and claim would have expired by now.
        await asyncio.sleep(ttl + heartbeat_interval(ttl))
        assert json.loads(await probe.get(presence_key))["beat_at"] > before
        assert bus_module.presence_fresh(await probe.pttl(presence_key), ttl)
        assert await probe.get(settings.slot_key(ident.name)) == bus._claim_token
    finally:
        # The life's end, which the stop runs after its shutdown handlers.
        await _stop(task)

    # The heartbeat is joined before the claim release, then the claim, the row and its index
    # member are gone.
    assert heartbeat_alive_at_release == [False]
    assert _live_heartbeat_threads() == []
    assert bus._heartbeat is None
    assert bus._presence is None
    assert await probe.exists(settings.slot_key(ident.name)) == 0
    assert await probe.get(presence_key) is None
    assert ident.name not in await probe.smembers(settings.presence_index)
    await probe.aclose()


async def test_a_heartbeat_that_ends_after_the_subscription_is_logged_when_it_ends(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # With the subscription ended on purpose nothing recovers a heartbeat that ends on its own,
    # so its end is logged at ERROR, with its cause, at the moment it ends — before the life's
    # end at ``leave_census`` — and that join logs it no second time.
    ttl = 0.6
    bus = make_bus(heartbeat_ttl=ttl)
    task, ident = await _spawn_subscriber(bus, _noop_op)
    heartbeat = bus._heartbeat
    assert heartbeat is not None
    caplog.set_level(logging.ERROR, logger="tai42_skeleton.app.bus.life")
    try:
        await _end_subscription(task)
        # The slot is lost: the next beat's renew misses and the heartbeat ends with SlotLostError.
        probe: Any = aioredis.FakeRedis(server=server, decode_responses=True)
        await probe.delete(bus._settings.slot_key(ident.name))
        await probe.aclose()
        # The end is logged by a callback on ``done``, which runs after ``done`` resolves.
        await asyncio.wait_for(_until(lambda: any(r.levelno == logging.ERROR for r in caplog.records)), timeout=2.0)

        assert heartbeat.done.done()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, [r.getMessage() for r in errors]
        assert ident.name in errors[0].getMessage()
        assert errors[0].exc_info is not None
        assert isinstance(errors[0].exc_info[1], bus_module.SlotLostError)
    finally:
        await _stop(task)

    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


async def test_leaving_the_census_leaves_a_reclaimed_slot_and_its_row_untouched(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    bus = make_bus(heartbeat_ttl=5.0)
    task, ident = await _spawn_subscriber(bus, _noop_op)
    settings = bus._settings
    presence_key = settings.presence_key(ident.name)
    probe: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await _end_subscription(task)
        # A later life already holds the slot and wrote its own row.
        await probe.set(settings.slot_key(ident.name), "foreign-token", px=15000)
        await probe.set(presence_key, '{"new":"holder"}', px=15000)
    finally:
        with caplog.at_level(logging.WARNING):
            await _stop(task)

    assert await probe.get(settings.slot_key(ident.name)) == "foreign-token"
    assert await probe.get(presence_key) == '{"new":"holder"}'
    assert ident.name in await probe.smembers(settings.presence_index)
    assert any("already reclaimed on stop" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert bus._presence is None
    await probe.aclose()


async def test_leaving_the_census_leaves_the_keys_to_their_ttl_on_a_transport_error(
    wire_bus_client: None,
    server: fakeredis.FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The life ends in the lifespan's shutdown, so a release blip is logged and the keys left to
    # their TTL, never raised over the shutdown's remaining steps.
    bus = make_bus(heartbeat_ttl=5.0)
    task, ident = await _spawn_subscriber(bus, _noop_op)
    settings = bus._settings

    async def blip(*_a: object, **_k: object) -> bool:
        raise RedisConnectionError("claim release lost its connection")

    try:
        await _end_subscription(task)
        monkeypatch.setattr(bus, "_release_claim", blip)
    finally:
        with caplog.at_level(logging.WARNING):
            await _stop(task)  # must NOT raise

    assert any(
        "claim release for" in r.getMessage() and "transport" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
    assert not any("already reclaimed" in r.getMessage() for r in caplog.records)
    assert _live_heartbeat_threads() == []
    assert bus._presence is None
    probe: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    assert await probe.get(settings.slot_key(ident.name)) == bus._claim_token
    assert await probe.exists(settings.presence_key(ident.name)) == 1
    await probe.aclose()


def _wire_a_presence_delete_raising(
    monkeypatch: pytest.MonkeyPatch, server: fakeredis.FakeServer, error: Exception
) -> None:
    """Route the bus's next connections to a handle whose transaction (the presence delete) raises ``error``."""

    @asynccontextmanager
    async def ctx(client_cls: object, settings: object = None, **_k: object) -> AsyncIterator[KitFakeRedis]:
        client = KitFakeRedis(server=server, decode_responses=True)

        def pipeline(**_kw: object) -> Any:
            class _Raising:
                def delete(self, *_a: object) -> None:
                    return None

                def srem(self, *_a: object) -> None:
                    return None

                async def execute(self) -> None:
                    raise error

            return _Raising()

        client.pipeline = pipeline  # type: ignore[method-assign]
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", ctx)


async def test_leaving_the_census_leaves_the_row_to_its_ttl_on_a_presence_delete_transport_error(
    wire_bus_client: None,
    server: fakeredis.FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = make_bus(heartbeat_ttl=5.0)
    task, ident = await _spawn_subscriber(bus, _noop_op)
    settings = bus._settings
    try:
        await _end_subscription(task)
        _wire_a_presence_delete_raising(
            monkeypatch, server, RedisConnectionError("presence delete lost its connection")
        )
    finally:
        with caplog.at_level(logging.WARNING):
            await _stop(task)  # must NOT raise

    assert any("presence delete for" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert bus._presence is None
    probe: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    assert await probe.exists(settings.presence_key(ident.name)) == 1
    await probe.aclose()


async def test_leaving_the_census_raises_a_presence_delete_error_that_is_not_a_transport_error(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only a transport blip is left to the TTL; any other failure of a leave step is the stop's to raise.
    bus = make_bus(heartbeat_ttl=5.0)
    task, _ident = await _spawn_subscriber(bus, _noop_op)
    await _end_subscription(task)
    _wire_a_presence_delete_raising(monkeypatch, server, ResponseError("a server-side error on the delete"))

    with pytest.raises(ResponseError, match="a server-side error on the delete"):
        await _stop(task)

    assert _live_heartbeat_threads() == []
    assert bus._presence is None


async def test_leaving_the_census_raises_a_claim_release_error_that_is_not_a_transport_error(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = make_bus(heartbeat_ttl=5.0)
    task, _ident = await _spawn_subscriber(bus, _noop_op)

    async def a_release_the_replica_refuses(*_a: object, **_k: object) -> bool:
        raise ReadOnlyError("You can't write against a read only replica.")

    await _end_subscription(task)
    monkeypatch.setattr(bus, "_release_claim", a_release_the_replica_refuses)

    with pytest.raises(ReadOnlyError):
        await _stop(task)

    assert _live_heartbeat_threads() == []
    assert bus._presence is None


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: WorkerBus.local(WorkerKind.serve), id="busless"),
        pytest.param(_make_nonmember_bus, id="fork-child"),
    ],
)
async def test_leaving_the_census_is_a_noop_without_a_member_life(
    build: Any, recording_bus_client: list[_RecordingRedis]
) -> None:
    bus = build()
    await bus.leave_census()
    assert recording_bus_client == []


async def test_leaving_the_census_is_a_noop_before_the_first_claim(
    recording_bus_client: list[_RecordingRedis],
) -> None:
    bus = make_bus()
    bus._identity = None  # an as-yet-unclaimed member (pre-subscribe)
    await bus.leave_census()
    assert recording_bus_client == []


async def test_a_transport_exit_stops_the_heartbeat_and_leaves_the_keys_to_their_ttl(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # The connection ends, not the life: the heartbeat stops, the keys carry the identity
    # across on their TTL, and the next connect keeps the same name and generation.
    bus = make_bus(heartbeat_ttl=5.0)
    readies: list[str] = []

    async def on_ready() -> None:
        readies.append("ready")

    caplog.set_level(logging.INFO, logger="tai42_skeleton.app.bus.subscribe")
    task = _start(bus, _noop_op, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(readies) == 1), timeout=2.0)
        first = bus.identity
        server.connected = False
        # The connection's end drops its presence as its last step, after stopping the heartbeat.
        await asyncio.wait_for(_until(lambda: bus._presence is None), timeout=3.0)
        assert _live_heartbeat_threads() == []
        assert bus._heartbeat is None
        assert any("transport exit" in r.getMessage() and "left to their TTL" in r.getMessage() for r in caplog.records)
        server.connected = True
        await asyncio.wait_for(_until(lambda: len(readies) == 2), timeout=3.0)
        assert (bus.identity.name, bus.identity.generation) == (first.name, first.generation)
    finally:
        await _stop(task)
