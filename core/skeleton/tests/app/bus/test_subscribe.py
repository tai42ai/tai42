"""Subscriber-side behaviour: the op-handling echo-skip/target filter, heartbeat supervision,
reconnect + re-mint, the boot resync's presence states, the last-op write, and fork
non-member derivation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_kit.clients.impl.redis import KitAsyncRedis

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app.bus import (
    LocalApplyResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerState,
)
from tai42_skeleton.app.bus.life import _Heartbeat
from tai42_skeleton.app.bus.models import _OP_PAYLOAD_KEY
from tai42_skeleton.app.bus.ops import ReloadConfig, ReloadMcp

from ..._fakes.kit_redis import KitFakeRedis
from ..._helpers import another_skeleton_major
from .conftest import _noop_op, _spawn_subscriber, _start, _stop, _until, make_bus

_RELOAD = ReloadConfig(op="reload_config")
_RELOAD_WIRE = {"op": "reload_config"}


def _live_heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("tai-worker-bus-heartbeat-")]


# -- workers of two skeleton majors on one namespace ---------------------------


async def test_workers_of_two_majors_on_one_namespace_are_two_fleets(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: dict[str, list[dict]] = {"running": [], "other": []}

    def _recording(cohort: str) -> Any:
        async def apply(op: dict, *, deadline: float) -> None:
            applied[cohort].append(op)

        return apply

    running = make_bus(kind=WorkerKind.serve)
    running_publisher = make_bus(kind=WorkerKind.serve)
    with another_skeleton_major(monkeypatch):
        other = make_bus(kind=WorkerKind.backend)
        other_publisher = make_bus(kind=WorkerKind.serve)
    t1, running_id = await _spawn_subscriber(running, _recording("running"))
    t2, other_id = await _spawn_subscriber(other, _recording("other"))
    try:
        assert [row.name for row in await running.census()] == [running_id.name]
        assert [row.name for row in await other.census()] == [other_id.name]

        local = LocalApplyResult(outcome=OpOutcome.applied)
        running_result = await running_publisher.publish(_RELOAD, targets=None, local=local)
        other_result = await other_publisher.publish(_RELOAD, targets=None, local=local)

        assert {r.name for r in running_result.results} == {running_publisher.identity.name, running_id.name}
        assert {r.name for r in other_result.results} == {other_publisher.identity.name, other_id.name}
        # Each subscriber's one channel delivers in publish order, so the op published on the
        # running major's fleet, published first, would have reached the other worker first.
        assert len(applied["running"]) == 1
        assert len(applied["other"]) == 1
    finally:
        await _stop(t1, t2)


# -- echo-skip + op-frame admission -------------------------------------------


async def test_echo_skip_is_keyed_on_name_and_generation() -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=3)
    applied: list[dict] = []

    async def record(op: dict, *, deadline: float) -> None:
        applied.append(op)

    def frame(name: str, generation: int) -> dict:
        return {"data": json.dumps({"payload": _RELOAD_WIRE, "name": name, "generation": generation})}

    # Own (name, generation): echo-skipped.
    await bus._handle_op(None, record, frame("serve-1", 3), failure_payload=lambda _op, _failure: None)
    assert applied == []

    # SAME name, EARLIER generation: a prior life's frame is foreign and applied.
    await bus._handle_op(None, record, frame("serve-1", 2), failure_payload=lambda _op, _failure: None)
    assert applied == [_RELOAD_WIRE]

    # A different name is foreign and applied.
    await bus._handle_op(None, record, frame("serve-2", 3), failure_payload=lambda _op, _failure: None)
    assert applied == [_RELOAD_WIRE, _RELOAD_WIRE]

    # A malformed frame is discarded without applying.
    await bus._handle_op(None, record, {"data": "not valid json"}, failure_payload=lambda _op, _failure: None)
    assert applied == [_RELOAD_WIRE, _RELOAD_WIRE]


# -- fork non-member derivation ------------------------------------------------


def test_fork_child_derives_a_nonmember_identity() -> None:
    # The at-fork hook re-derives a claimed member parent's identity into an explicit
    # non-member: {parent}/fork-{pid}, generation 0, member False — never a collision.
    bus = make_bus(kind=WorkerKind.backend)
    bus._identity = WorkerIdentity(name="backend-2", kind=WorkerKind.backend, pid=1, generation=7)
    bus._fork_child_nonmember()
    child = bus.identity
    assert child.name == f"backend-2/fork-{os.getpid()}"
    assert child.generation == 0
    assert child.member is False
    assert child.kind == WorkerKind.backend


async def test_fork_child_does_not_inherit_its_parents_departure(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exit latch is one per process life: a child forked from a parent already leaving
    # starts with none of the parent's departure, nor its announce.
    import tai42_skeleton.app.bus.worker_bus as worker_bus_module

    monkeypatch.setattr(worker_bus_module.os, "kill", lambda pid, sig: None)
    bus = make_bus(kind=WorkerKind.backend)
    bus._identity = WorkerIdentity(name="backend-2", kind=WorkerKind.backend, pid=1, generation=7)
    bus.request_graceful_exit("the parent leaves")
    assert bus.departing == "the parent leaves"
    bus._fork_child_nonmember()
    assert bus.departing is None
    assert bus._leaving_announce is None


def test_a_member_acts_for_itself() -> None:
    bus = make_bus(kind=WorkerKind.backend)
    bus._identity = WorkerIdentity(name="backend-2", kind=WorkerKind.backend, pid=11, generation=7)
    assert bus.member_identity is bus.identity


def test_fork_child_keeps_the_member_it_acts_for_across_further_forks() -> None:
    # A forked job child is a non-member, but it acts for the member it was forked from: the
    # writer's role and its post-reply exit belong to that member. A child that forks again
    # still acts for the same member, the top of its lineage.
    bus = make_bus(kind=WorkerKind.backend)
    member = WorkerIdentity(name="backend-2", kind=WorkerKind.backend, pid=11, generation=7)
    bus._identity = member
    bus._fork_child_nonmember()
    assert bus.identity.member is False
    assert bus.member_identity == member
    bus._fork_child_nonmember()
    assert bus.identity.member is False
    assert bus.identity.name == f"backend-2/fork-{os.getpid()}/fork-{os.getpid()}"
    assert bus.member_identity == member


def test_fork_child_of_a_local_bus_acts_for_the_local_identity() -> None:
    bus = WorkerBus.local(WorkerKind.serve)
    local = bus.identity
    bus._fork_child_nonmember()
    assert bus.member_identity == local


def test_fork_child_drops_the_inherited_heartbeat_thread() -> None:
    # A fork child inherits only the forking thread: the parent's heartbeat thread does
    # not exist there, so the child drops the inherited handle and never stops or joins it.
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    key = bus._settings.presence_key("serve-1")
    bus._heartbeat = _Heartbeat("tai-worker-bus-heartbeat-serve-1", lambda: bus._heartbeat_main(key))
    bus._fork_child_nonmember()
    assert bus._heartbeat is None


def test_fork_derive_from_a_local_bus_succeeds() -> None:
    # A local bus always holds {kind}-1 from construction, so a fork-derive succeeds.
    bus = WorkerBus.local(WorkerKind.serve)
    bus._fork_child_nonmember()
    child = bus.identity
    assert child.name == f"serve-1/fork-{os.getpid()}"
    assert child.generation == 0
    assert child.member is False


async def test_fork_from_an_unclaimed_member_poisons_the_identity(wire_bus_client: None) -> None:
    # A member parent that has NOT yet claimed has no name to derive from; the in-hook
    # raise is unraisable, so the child's identity is POISONED and any bus USE raises
    # loudly at the use site (asserted through the production effect: a publish).
    bus = make_bus()
    bus._identity = None  # an as-yet-unclaimed member parent (pre-subscribe)
    bus._fork_child_nonmember()  # simulate the after-in-child hook firing in the child
    assert bus._poisoned is True
    with pytest.raises(RuntimeError, match="had not yet claimed"):
        _ = bus.identity
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(RuntimeError, match="had not yet claimed"):
        await bus.publish(_RELOAD, targets=None, local=local)


# -- reconnect / resubscribe after transport failure --------------------------


async def test_reconnect_after_transport_drop(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    worker = make_bus(heartbeat_ttl=5.0)
    reconnects: list[str] = []

    async def apply(op: dict, *, deadline: float) -> None:
        return None

    async def on_ready() -> None:
        reconnects.append("ready")

    task = _start(worker, apply, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(reconnects) >= 1), timeout=2.0)
        name = worker.identity.name
        # Drop the transport: the next poll raises ConnectionError inside the loop.
        server.connected = False
        await asyncio.sleep(0.15)
        # Restore and let the reconnect loop re-verify the (still-held) claim + re-fire ready.
        server.connected = True
        await asyncio.wait_for(_until(lambda: len(reconnects) >= 2), timeout=3.0)

        # The claim survived the blip (heartbeat_ttl 5s), so the identity is unchanged.
        assert worker.identity.name == name
        census_names = {row.name for row in await publisher.census()}
        assert worker.identity.name in census_names
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker.identity.name].outcome == OpOutcome.applied
    finally:
        await _stop(task)


async def _row_of(publisher: WorkerBus, name: str) -> Any:
    return {r.name: r for r in await publisher.census()}.get(name)


@pytest.mark.parametrize("begun_while", ["connected", "disconnected"])
async def test_a_life_that_began_leaving_still_reads_recycling_after_a_reconnect(
    wire_bus_client: None, server: fakeredis.FakeServer, begun_while: str
) -> None:
    # The row is the published form of the life's leaving: a reconnect's fresh presence and its
    # resync keep it ``recycling``, with the declared drain, whether the leaving began while the
    # row was written or while the transport was down and nothing could be written.
    publisher = make_bus()
    worker = make_bus(heartbeat_ttl=5.0, drain=630.0)
    readies: list[str] = []

    async def on_ready() -> None:
        readies.append("ready")

    task = _start(worker, _noop_op, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(readies) >= 1), timeout=2.0)
        name = worker.identity.name
        if begun_while == "connected":
            assert worker.stop_began("SIGTERM received") is True
            await worker.leaving_announced()
            assert (await _row_of(publisher, name)).state == WorkerState.recycling
        server.connected = False
        await asyncio.sleep(0.15)
        if begun_while == "disconnected":
            assert worker.stop_began("SIGTERM received") is True
            await worker.leaving_announced()
        server.connected = True
        await asyncio.wait_for(_until(lambda: len(readies) >= 2), timeout=3.0)
        # The reconnect's resync has written its last state.
        await asyncio.sleep(0.1)

        row = await _row_of(publisher, name)
        assert (row.state, row.drain_seconds) == (WorkerState.recycling, 630.0)
        assert worker.departing == "SIGTERM received"
    finally:
        await _stop(task)


async def test_a_reconnect_the_retry_hides_is_followed_by_the_resync(
    wire_bus_client: None,
    server: fakeredis.FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The subscriber's own connection drops and its next read reconnects and
    # re-subscribes inside the call, so no error reaches the loop. An op published
    # while it was disconnected is never delivered, so the resync runs again.
    pubsubs: list[Any] = []
    kit_pubsub = KitAsyncRedis.pubsub

    def recording_pubsub(self: KitAsyncRedis, **kwargs: Any) -> Any:
        pubsub = kit_pubsub(self, **kwargs)
        pubsubs.append(pubsub)
        return pubsub

    monkeypatch.setattr(KitFakeRedis, "pubsub", recording_pubsub)
    publisher = make_bus(apply_timeout=_REPLY_WINDOW)
    worker = make_bus(heartbeat_ttl=5.0)
    readies: list[str] = []
    applied: list[dict] = []
    gap_op = {"op": "reload_mcp"}

    async def apply(op: dict, *, deadline: float) -> None:
        applied.append(op)
        if op == _RELOAD_WIRE:
            # Drop only the subscriber's connection (the first pub/sub built; the
            # publisher's reply pub/sub comes after it), then publish while it is down.
            await pubsubs[0].connection.disconnect()
            raw = KitFakeRedis(server=server, decode_responses=True)
            frame = {
                "name": "serve-gap",
                "generation": 1,
                "op_id": "gap",
                "reply_to": "tai:gap-reply",
                "targets": None,
                _OP_PAYLOAD_KEY: gap_op,
            }
            await raw.publish(worker._settings.channel, json.dumps(frame))
            await raw.aclose()

    async def on_ready() -> None:
        readies.append(worker.identity.name)

    caplog.set_level(logging.WARNING, logger="tai42_skeleton.app.bus.subscribe")
    task = _start(worker, apply, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(readies) == 1), timeout=2.0)
        name = worker.identity.name
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        assert {r.name: r for r in result.results}[name].outcome == OpOutcome.applied

        await asyncio.wait_for(_until(lambda: len(readies) == 2), timeout=3.0)

        async def _ready() -> bool:
            rows = {r.name: r for r in await publisher.census()}
            return name in rows and rows[name].state == WorkerState.ready

        async with asyncio.timeout(2.0):
            while not await _ready():
                await asyncio.sleep(0.01)
        assert readies == [name, name]
        assert gap_op not in applied
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert [r for r in caplog.records if "resync" in r.getMessage() and r.levelno == logging.WARNING]
    finally:
        await _stop(task)


async def test_reconnect_after_wrapped_disconnect(wire_pooled_bus_client: None, server: fakeredis.FakeServer) -> None:
    # The subscription's transport error arrives WRAPPED too: the pooled ``client_ctx``
    # folds the severed poll into ``ClientDisconnectedError``. The reconnect loop must
    # treat that wrapped type as transient exactly like the raw ``ConnectionError`` —
    # re-verify the claim, re-register presence, re-fire ready — never let it kill the task.
    publisher = make_bus()
    worker = make_bus(heartbeat_ttl=5.0)
    reconnects: list[str] = []

    async def apply(op: dict, *, deadline: float) -> None:
        return None

    async def on_ready() -> None:
        reconnects.append("ready")

    task = _start(worker, apply, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(reconnects) >= 1), timeout=2.0)
        server.connected = False
        await asyncio.sleep(0.15)
        server.connected = True
        await asyncio.wait_for(_until(lambda: len(reconnects) >= 2), timeout=3.0)

        census_names = {row.name for row in await publisher.census()}
        assert worker.identity.name in census_names
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker.identity.name].outcome == OpOutcome.applied
    finally:
        await _stop(task)


# -- heartbeat supervision: an asymmetric refresh failure forces reconnect ----


async def test_heartbeat_death_forces_reconnect(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Model an ASYMMETRIC transport failure: the presence-refresh heartbeat raises
    # (its pooled command connection drops) while the held pub/sub message loop stays
    # healthy. Supervised, its death tears the subscription down so the outer reconnect
    # loop re-subscribes, re-verifies the claim, and RESTARTS the heartbeat.
    publisher = make_bus()
    worker = make_bus(heartbeat_ttl=5.0)
    readies: list[str] = []
    heartbeats = {"n": 0}
    beat_threads: list[threading.Thread] = []
    real_heartbeat = worker._heartbeat_loop

    async def flaky_heartbeat(r: object, presence_key: str) -> None:
        heartbeats["n"] += 1
        beat_threads.append(threading.current_thread())
        if heartbeats["n"] == 1:
            await asyncio.sleep(0.05)
            raise RedisConnectionError("presence refresh lost its pooled connection")
        await real_heartbeat(r, presence_key)

    monkeypatch.setattr(worker, "_heartbeat_loop", flaky_heartbeat)

    async def apply(_op: dict, *, deadline: float) -> None:
        return None

    async def on_ready() -> None:
        readies.append("ready")

    task = _start(worker, apply, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(readies) >= 1), timeout=10.0)
        await asyncio.wait_for(_until(lambda: len(readies) >= 2), timeout=10.0)
        await asyncio.wait_for(_until(lambda: heartbeats["n"] >= 2), timeout=10.0)
        # The failure raised on the heartbeat thread; the reconnect started a NEW thread.
        assert beat_threads[0] is not beat_threads[1]
        assert threading.main_thread() not in beat_threads
        assert not beat_threads[0].is_alive()

        census_names = {row.name for row in await publisher.census()}
        assert worker.identity.name in census_names
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker.identity.name].outcome == OpOutcome.applied
    finally:
        await _stop(task)
    assert _live_heartbeat_threads() == []


# -- lost slot: SlotLostError re-mints a new life -----------------------------


async def test_lost_slot_remints_a_new_life(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # A renew miss on a HEALTHY connection is a lost slot, not a transport outage: the
    # heartbeat raises SlotLostError, the dedicated reconnect branch re-enters, and a
    # NEW life is minted (lowest free ordinal since the old name is now foreign-held).
    worker = make_bus(heartbeat_ttl=0.3)
    readies: list[str] = []

    async def apply(_op: dict, *, deadline: float) -> None:
        return None

    async def on_ready() -> None:
        readies.append("ready")

    task = _start(worker, apply, on_ready)
    try:
        await asyncio.wait_for(_until(lambda: len(readies) >= 1), timeout=3.0)
        first = worker.identity.name
        assert first == "serve-1"
        # Steal the claim under a foreign token: the next heartbeat renew misses.
        stealer = aioredis.FakeRedis(server=server, decode_responses=True)
        with caplog.at_level(logging.ERROR):
            await stealer.set(worker._settings.slot_key("serve-1"), "foreign-token", px=30000)
            await stealer.aclose()
            # The re-mint drives a second on_ready and a new name (serve-1 is foreign-held).
            await asyncio.wait_for(_until(lambda: len(readies) >= 2), timeout=5.0)
            await asyncio.wait_for(_until(lambda: worker.identity.name == "serve-2"), timeout=5.0)
        assert worker.identity.generation == 1
        # SlotLostError routes through its DEDICATED no-backoff branch, NOT the transport
        # tuple: no "subscription transport error" reconnect log is emitted for a lost slot.
        assert not any("subscription transport error" in r.getMessage() for r in caplog.records)
    finally:
        await _stop(task)
    assert _live_heartbeat_threads() == []


# -- presence value round-trip + last_op stamp --------------------------------


async def test_presence_value_round_trips_through_census(wire_bus_client: None) -> None:
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        rows = {r.name: r for r in await bus.census()}
        row = rows[identity.name]
        assert row.generation == identity.generation
        assert row.kind == WorkerKind.serve
        assert row.state == WorkerState.ready
        assert row.joined_at
        assert row.beat_at
        assert row.last_op is None
    finally:
        await _stop(task)


async def test_last_op_is_stamped_after_a_terminal_reply(wire_bus_client: None) -> None:
    publisher = make_bus(heartbeat_ttl=5.0)
    worker = make_bus(heartbeat_ttl=5.0)

    async def apply(_op: dict, *, deadline: float) -> dict:
        return {"ok": True}

    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        await publisher.publish(_RELOAD, targets=None, local=local)

        async def _stamped() -> bool:
            rows = {r.name: r for r in await publisher.census()}
            return rows[worker_id.name].last_op is not None

        async with asyncio.timeout(2.0):
            while not await _stamped():
                await asyncio.sleep(0.01)
        rows = {r.name: r for r in await publisher.census()}
        last_op = rows[worker_id.name].last_op
        assert last_op is not None
        assert last_op.op == "reload_config"
        assert last_op.outcome == "applied"
        assert last_op.at
    finally:
        await _stop(task)


async def test_the_callback_deadline_is_the_receipt_instant_plus_the_apply_window(wire_bus_client: None) -> None:
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=7.0)
    worker = make_bus(heartbeat_ttl=5.0, apply_timeout=7.0)
    deadlines: list[float] = []

    async def apply(_op: dict, *, deadline: float) -> None:
        deadlines.append(deadline)

    task, _worker_id = await _spawn_subscriber(worker, apply)
    try:
        before = time.monotonic()
        local = LocalApplyResult(outcome=OpOutcome.applied)
        await publisher.publish(_RELOAD, targets=None, local=local)
        after = time.monotonic()
        assert len(deadlines) == 1
        assert before + 7.0 <= deadlines[0] <= after + 7.0
    finally:
        await _stop(task)


# -- boot/re-entry ordering: resyncing BEFORE on_ready, ready AFTER -----------


async def test_presence_state_is_resyncing_before_on_ready_and_ready_after(wire_bus_client: None) -> None:
    # A booting worker advertises resyncing in presence BEFORE its resync (on_ready)
    # runs, then flips to ready only AFTER on_ready converges.
    bus = make_bus(heartbeat_ttl=5.0)
    state_during_ready: list[WorkerState] = []

    async def on_ready() -> None:
        rows = {r.name: r for r in await bus.census()}
        state_during_ready.append(rows[bus.identity.name].state)

    task = _start(bus, _noop_op, on_ready)
    try:
        async with asyncio.timeout(2.0):
            while not state_during_ready:
                await asyncio.sleep(0.01)
        assert state_during_ready[0] == WorkerState.resyncing

        async def _ready() -> bool:
            rows = {r.name: r for r in await bus.census()}
            return bus.identity.name in rows and rows[bus.identity.name].state == WorkerState.ready

        async with asyncio.timeout(2.0):
            while not await _ready():
                await asyncio.sleep(0.01)
    finally:
        await _stop(task)


# -- worst-outcome-wins terminal merge (apply path) ---------------------------


async def test_failing_callback_reports_failed(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker = make_bus()

    async def boom(_op: dict, *, deadline: float) -> None:
        raise RuntimeError("nope")

    task, worker_id = await _spawn_subscriber(worker, boom)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.failed
        assert "nope" in (by_name[worker_id.name].error or "")
        assert by_name[worker_id.name].payload is None
        assert not result.ok
    finally:
        await _stop(task)


async def test_a_failed_reply_carries_the_payload_the_subscriber_states_for_its_failure(
    wire_bus_client: None,
) -> None:
    publisher = make_bus()
    worker = make_bus()
    refusal = RuntimeError("refused at the build")
    stated: list[tuple[dict, Exception]] = []

    async def refuse(_op: dict, *, deadline: float) -> None:
        raise refusal

    def statement(op: dict, failure: Exception) -> dict[str, Any]:
        stated.append((op, failure))
        return {"status": "discarded", "awaiting_respawn": ["TAI_X"]}

    task, worker_id = await _spawn_subscriber(worker, refuse, failure_payload=statement)
    try:
        result = await publisher.publish(_RELOAD, targets=None, local=LocalApplyResult(outcome=OpOutcome.applied))
        row = {r.name: r for r in result.results}[worker_id.name]
        assert row.outcome == OpOutcome.failed
        assert row.payload == {"status": "discarded", "awaiting_respawn": ["TAI_X"]}
        assert row.error == "RuntimeError: refused at the build"
        assert stated == [(_RELOAD_WIRE, refusal)]
    finally:
        await _stop(task)


async def test_a_failed_reply_the_subscriber_states_nothing_for_carries_no_payload(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker = make_bus()

    async def boom(_op: dict, *, deadline: float) -> None:
        raise RuntimeError("nope")

    task, worker_id = await _spawn_subscriber(worker, boom, failure_payload=lambda _op, _failure: None)
    try:
        result = await publisher.publish(_RELOAD, targets=None, local=LocalApplyResult(outcome=OpOutcome.applied))
        row = {r.name: r for r in result.results}[worker_id.name]
        assert row.outcome == OpOutcome.failed
        assert row.payload is None
    finally:
        await _stop(task)


# -- subscriber-side targets filter -------------------------------------------


async def test_subscriber_skips_op_outside_targets(wire_bus_client: None) -> None:
    # The publisher returns on the targeted worker's terminal reply, so its reply windows only
    # bound a hang; they are generous so a slow host still collects the ``applied`` reply.
    publisher = make_bus(apply_timeout=_REPLY_WINDOW)
    worker_a = make_bus()
    worker_b = make_bus()
    a_seen: list[dict] = []
    b_seen: list[dict] = []

    async def record_a(op: dict, *, deadline: float) -> None:
        a_seen.append(op)

    async def record_b(op: dict, *, deadline: float) -> None:
        b_seen.append(op)

    task_a, id_a = await _spawn_subscriber(worker_a, record_a)
    task_b, id_b = await _spawn_subscriber(worker_b, record_b)
    try:
        result = await publisher.publish(_RELOAD, targets=[id_a.name], local=None)
        by_name = {r.name: r for r in result.results}
        assert set(by_name) == {id_a.name}
        assert id_b.name not in by_name
        assert by_name[id_a.name].outcome == OpOutcome.applied
        assert a_seen == [_RELOAD_WIRE]
        # B reads the op stream in order: once B has applied an op published AFTER the first,
        # the first has passed B by, so B holding only the later op proves it skipped the first.
        later = await publisher.publish(ReloadMcp(op="reload_mcp", title="after"), targets=[id_b.name], local=None)
        assert {r.name: r.outcome for r in later.results} == {id_b.name: OpOutcome.applied}
        assert b_seen == [{"op": "reload_mcp", "title": "after"}]
    finally:
        await _stop(task_a, task_b)


async def test_empty_targets_list_reaches_nobody(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker_a = make_bus()
    worker_b = make_bus()
    a_seen: list[dict] = []
    b_seen: list[dict] = []

    async def record_a(op: dict, *, deadline: float) -> None:
        a_seen.append(op)

    async def record_b(op: dict, *, deadline: float) -> None:
        b_seen.append(op)

    task_a, _ = await _spawn_subscriber(worker_a, record_a)
    task_b, _ = await _spawn_subscriber(worker_b, record_b)
    try:
        result = await publisher.publish(_RELOAD, targets=[], local=None)
        assert result.results == []
        await asyncio.sleep(0.05)
        assert a_seen == []
        assert b_seen == []
    finally:
        await _stop(task_a, task_b)


# -- the subscription connection is epoch-immune (fresh) ----------------------


async def test_subscription_uses_a_fresh_epoch_immune_connection(
    monkeypatch: pytest.MonkeyPatch, server: fakeredis.FakeServer
) -> None:
    """The process-lifetime subscription and its heartbeat thread must acquire their
    connections with ``fresh=True`` so they live OUTSIDE the epoch client pool. Guards
    the exact ``fresh=`` argument at both call sites."""
    fresh_flags: list[bool] = []

    @asynccontextmanager
    async def spy(client_cls, settings=None, *, fresh: bool = False, **kw) -> AsyncIterator[KitFakeRedis]:
        fresh_flags.append(fresh)
        client = KitFakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", spy)

    bus = make_bus()
    ready = asyncio.Event()

    async def on_ready() -> None:
        ready.set()

    async def apply(_op: dict, *, deadline: float) -> object:
        return None

    task = _start(bus, apply, on_ready)
    try:
        await asyncio.wait_for(ready.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        # A bare subscribe opens two connections — the subscription's (pub/sub, claim and
        # loop-side presence writes) and its heartbeat thread's — and both must be fresh.
        assert fresh_flags == [True, True], f"a bus connection was not fresh=True: {fresh_flags}"
    finally:
        await _stop(task)


# The publisher's ack and apply windows in a test that waits on a terminal reply: a hang bound.
_REPLY_WINDOW = 30.0
