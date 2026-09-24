"""Subscriber-side behaviour: slot claim/renew/release, identity re-establish, the
op-handling echo-skip/target filter, heartbeat supervision, reconnect + re-mint,
teardown, presence + last-op writes, and fork non-member derivation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fakeredis
import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app.bus import (
    LocalApplyResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerState,
    _PresenceValue,
)

from .conftest import _noop_op, _RecordingRedis, _spawn_subscriber, _stop, _until, make_bus

# -- slot claim / generation / ownership (compare-token) ----------------------


async def test_two_serve_workers_claim_distinct_ordinals(wire_bus_client: None) -> None:
    bus_a = make_bus(kind=WorkerKind.serve)
    bus_b = make_bus(kind=WorkerKind.serve)

    async def noop(_op: dict) -> None:
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


# -- echo-skip + op-frame admission -------------------------------------------


async def test_echo_skip_is_keyed_on_name_and_generation() -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=3)
    applied: list[dict] = []

    async def record(op: dict) -> None:
        applied.append(op)

    def frame(name: str, generation: int) -> dict:
        return {"data": json.dumps({"payload": {"op": "reload_config"}, "name": name, "generation": generation})}

    # Own (name, generation): echo-skipped.
    await bus._handle_op(None, record, frame("serve-1", 3))
    assert applied == []

    # SAME name, EARLIER generation: a prior life's frame is foreign and applied.
    await bus._handle_op(None, record, frame("serve-1", 2))
    assert applied == [{"op": "reload_config"}]

    # A different name is foreign and applied.
    await bus._handle_op(None, record, frame("serve-2", 3))
    assert applied == [{"op": "reload_config"}, {"op": "reload_config"}]

    # A malformed frame is discarded without applying.
    await bus._handle_op(None, record, {"data": "not valid json"})
    assert applied == [{"op": "reload_config"}, {"op": "reload_config"}]


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
        await bus.publish({"op": "reload_config"}, targets=None, local=local)


# -- reconnect / resubscribe after transport failure --------------------------


async def test_reconnect_after_transport_drop(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    worker = make_bus(heartbeat_ttl=5.0)
    reconnects: list[str] = []

    async def apply(op: dict) -> None:
        return None

    async def on_ready() -> None:
        reconnects.append("ready")

    task = asyncio.create_task(worker.subscribe(apply, on_ready))
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
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker.identity.name].outcome == OpOutcome.applied
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

    async def apply(op: dict) -> None:
        return None

    async def on_ready() -> None:
        reconnects.append("ready")

    task = asyncio.create_task(worker.subscribe(apply, on_ready))
    try:
        await asyncio.wait_for(_until(lambda: len(reconnects) >= 1), timeout=2.0)
        server.connected = False
        await asyncio.sleep(0.15)
        server.connected = True
        await asyncio.wait_for(_until(lambda: len(reconnects) >= 2), timeout=3.0)

        census_names = {row.name for row in await publisher.census()}
        assert worker.identity.name in census_names
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
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
    real_heartbeat = worker._heartbeat_loop

    async def flaky_heartbeat(r: object, presence_key: str) -> None:
        heartbeats["n"] += 1
        if heartbeats["n"] == 1:
            await asyncio.sleep(0.05)
            raise RedisConnectionError("presence refresh lost its pooled connection")
        await real_heartbeat(r, presence_key)

    monkeypatch.setattr(worker, "_heartbeat_loop", flaky_heartbeat)

    async def apply(_op: dict) -> None:
        return None

    async def on_ready() -> None:
        readies.append("ready")

    task = asyncio.create_task(worker.subscribe(apply, on_ready))
    try:
        await asyncio.wait_for(_until(lambda: len(readies) >= 1), timeout=10.0)
        await asyncio.wait_for(_until(lambda: len(readies) >= 2), timeout=10.0)
        await asyncio.wait_for(_until(lambda: heartbeats["n"] >= 2), timeout=10.0)

        census_names = {row.name for row in await publisher.census()}
        assert worker.identity.name in census_names
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker.identity.name].outcome == OpOutcome.applied
    finally:
        await _stop(task)


# -- lost slot: SlotLostError re-mints a new life -----------------------------


async def test_lost_slot_remints_a_new_life(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # A renew miss on a HEALTHY connection is a lost slot, not a transport outage: the
    # heartbeat raises SlotLostError, the dedicated reconnect branch re-enters, and a
    # NEW life is minted (lowest free ordinal since the old name is now foreign-held).
    worker = make_bus(heartbeat_ttl=0.3)
    readies: list[str] = []

    async def apply(_op: dict) -> None:
        return None

    async def on_ready() -> None:
        readies.append("ready")

    task = asyncio.create_task(worker.subscribe(apply, on_ready))
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


# -- teardown split: deliberate releases the claim, transport/lost leave keys to TTL --


async def test_teardown_deliberate_releases_claim_and_deletes_presence(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        name = bus.identity.name
        pkey = bus._settings.presence_key(name)
        await client.set(pkey, "{}", px=15000)
        pubsub = client.pubsub()
        await pubsub.subscribe(bus._settings.channel)

        await bus._teardown(client, pubsub, pkey, "deliberate")

        # A deliberate stop releases the claim then deletes its own presence row.
        assert await client.exists(pkey) == 0
        assert await client.exists(bus._settings.slot_key(name)) == 0
    finally:
        await client.aclose()


async def test_teardown_transport_leaves_both_keys_on_ttl(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        name = bus.identity.name
        pkey = bus._settings.presence_key(name)
        await client.set(pkey, "{}", px=15000)
        pubsub = client.pubsub()
        await pubsub.subscribe(bus._settings.channel)

        await bus._teardown(client, pubsub, pkey, "transport")

        # A transport-error exit carries BOTH keys on their TTL across the reconnect.
        assert await client.exists(pkey) == 1
        assert await client.exists(bus._settings.slot_key(name)) == 1
    finally:
        await client.aclose()


async def test_teardown_deliberate_leaves_a_reclaimed_row_untouched(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        name = bus.identity.name
        pkey = bus._settings.presence_key(name)
        # A NEW holder already owns the slot + row (foreign claim token).
        await client.set(bus._settings.slot_key(name), "foreign-token", px=15000)
        await client.set(pkey, '{"new":"holder"}', px=15000)
        pubsub = client.pubsub()
        await pubsub.subscribe(bus._settings.channel)

        await bus._teardown(client, pubsub, pkey, "deliberate")

        # The release misses (foreign token), so the new holder's row is NOT deleted.
        assert await client.exists(pkey) == 1
        assert await client.get(bus._settings.slot_key(name)) == "foreign-token"
    finally:
        await client.aclose()


async def test_teardown_deliberate_releases_claim_before_presence_is_registered(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # A deliberate stop that cancels in the claim window — the slot is won but the
    # presence row is not yet written (presence_key None) — must STILL release the
    # claim, so the slot never lingers to its TTL.
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        name = bus.identity.name
        assert await client.exists(bus._settings.slot_key(name)) == 1
        pubsub = client.pubsub()
        await pubsub.subscribe(bus._settings.channel)

        with caplog.at_level(logging.WARNING):
            await bus._teardown(client, pubsub, None, "deliberate")

        # The compare-token release ran: the slot is free (a fresh SET NX now wins), and
        # no spurious "already reclaimed" warning fires with no presence row to leave.
        assert await client.set(bus._settings.slot_key(name), "next-token", nx=True) is True
        assert not any("already reclaimed" in r.getMessage() for r in caplog.records)
    finally:
        await client.aclose()


async def test_teardown_deliberate_swallows_a_release_transport_blip(
    wire_bus_client: None,
    server: fakeredis.FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A deliberate stop is a CancelledError propagating through the subscription's
    # finally → _teardown; a transport error on the claim release must be logged and the
    # claim left to its TTL, NEVER propagated — else it replaces the CancelledError and
    # the stop reconnect-loops instead of terminating. The pub/sub close and the
    # _presence reset still run.
    bus = make_bus()
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await bus._establish_identity(client)
        name = bus.identity.name
        pkey = bus._settings.presence_key(name)
        bus._presence = _PresenceValue(
            kind=WorkerKind.serve,
            pid=1,
            generation=1,
            joined_at="2026-01-01T00:00:00+00:00",
            beat_at="2026-01-01T00:00:00+00:00",
            state=WorkerState.ready,
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(bus._settings.channel)

        async def blip(*_a: object, **_k: object) -> bool:
            raise RedisConnectionError("claim release lost its connection")

        monkeypatch.setattr(bus, "_release_claim", blip)
        with caplog.at_level(logging.WARNING):
            await bus._teardown(client, pubsub, pkey, "deliberate")  # must NOT raise

        assert any(
            "claim release for" in r.getMessage() and "transport" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
        # The transport path does not emit the misleading "already reclaimed" warning.
        assert not any("already reclaimed" in r.getMessage() for r in caplog.records)
        # Teardown still completed: the in-memory presence is reset and the pub/sub is
        # unsubscribed/closed, and the claim key is left to its TTL.
        assert bus._presence is None
        assert pubsub.subscribed is False
        assert await client.exists(bus._settings.slot_key(name)) == 1
    finally:
        await client.aclose()


# -- recycling state written before graceful self-exit ------------------------


async def test_mark_recycling_writes_the_recycling_state(wire_bus_client: None) -> None:
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        await bus.mark_recycling()
        rows = {r.name: r for r in await bus.census()}
        assert rows[identity.name].state == WorkerState.recycling
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
    # The recycling-state write is cosmetic (it only tells the census WHY a worker is
    # departing), so a transport blip on it must be logged and swallowed, never
    # propagated — an unguarded raise here would abort the recycle op over a census
    # write. The renew succeeds; the presence SET raises a transport error.
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict) -> None:
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
        )
        # Steal the claim so every renew misses.
        await client.set(bus._settings.slot_key(name), "foreign-token", px=30000)

        await bus.mark_recycling()
        assert bus._presence.state == WorkerState.ready  # not flipped — the write was skipped

        await bus._stamp_last_op(client, "reload_config", "applied")
        assert bus._presence.last_op is None  # not stamped — the write was skipped
    finally:
        await client.aclose()


# -- presence value round-trip + last_op stamp --------------------------------


async def test_presence_value_round_trips_through_census(wire_bus_client: None) -> None:
    bus = make_bus(heartbeat_ttl=5.0)

    async def noop(_op: dict) -> None:
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

    async def apply(_op: dict) -> dict:
        return {"ok": True}

    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        await publisher.publish({"op": "reload_config"}, targets=None, local=local)

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


# -- boot/re-entry ordering: resyncing BEFORE on_ready, ready AFTER -----------


async def test_presence_state_is_resyncing_before_on_ready_and_ready_after(wire_bus_client: None) -> None:
    # A booting worker advertises resyncing in presence BEFORE its resync (on_ready)
    # runs, then flips to ready only AFTER on_ready converges.
    bus = make_bus(heartbeat_ttl=5.0)
    state_during_ready: list[WorkerState] = []

    async def on_ready() -> None:
        rows = {r.name: r for r in await bus.census()}
        state_during_ready.append(rows[bus.identity.name].state)

    task = asyncio.create_task(bus.subscribe(_noop_op, on_ready))
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

    async def boom(_op: dict) -> None:
        raise RuntimeError("nope")

    task, worker_id = await _spawn_subscriber(worker, boom)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.failed
        assert "nope" in (by_name[worker_id.name].error or "")
        assert not result.ok
    finally:
        await _stop(task)


# -- subscriber-side targets filter -------------------------------------------


async def test_subscriber_skips_op_outside_targets(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker_a = make_bus()
    worker_b = make_bus()
    a_seen: list[dict] = []
    b_seen: list[dict] = []

    async def record_a(op: dict) -> None:
        a_seen.append(op)

    async def record_b(op: dict) -> None:
        b_seen.append(op)

    task_a, id_a = await _spawn_subscriber(worker_a, record_a)
    task_b, id_b = await _spawn_subscriber(worker_b, record_b)
    try:
        result = await publisher.publish({"op": "reload_config"}, targets=[id_a.name], local=None)
        by_name = {r.name: r for r in result.results}
        assert set(by_name) == {id_a.name}
        assert id_b.name not in by_name
        assert by_name[id_a.name].outcome == OpOutcome.applied
        assert a_seen == [{"op": "reload_config"}]
        await asyncio.sleep(0.05)
        assert b_seen == []
    finally:
        await _stop(task_a, task_b)


async def test_empty_targets_list_reaches_nobody(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker_a = make_bus()
    worker_b = make_bus()
    a_seen: list[dict] = []
    b_seen: list[dict] = []

    async def record_a(op: dict) -> None:
        a_seen.append(op)

    async def record_b(op: dict) -> None:
        b_seen.append(op)

    task_a, _ = await _spawn_subscriber(worker_a, record_a)
    task_b, _ = await _spawn_subscriber(worker_b, record_b)
    try:
        result = await publisher.publish({"op": "reload_config"}, targets=[], local=None)
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
    """The process-lifetime subscription must acquire its connection with ``fresh=True``
    so it lives OUTSIDE the epoch client pool. Guards the exact ``fresh=`` argument at
    the subscription call site."""
    fresh_flags: list[bool] = []

    @asynccontextmanager
    async def spy(client_cls, settings=None, *, fresh: bool = False, **kw) -> AsyncIterator[aioredis.FakeRedis]:
        fresh_flags.append(fresh)
        client = aioredis.FakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", spy)

    bus = make_bus()
    ready = asyncio.Event()

    async def on_ready() -> None:
        ready.set()

    async def apply(_op: dict) -> object:
        return None

    task = asyncio.create_task(bus.subscribe(apply, on_ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        # The subscription (and its shared-connection heartbeat + claim) is the only
        # connection a bare subscribe opens, and it must be fresh.
        assert fresh_flags == [True], f"the subscription connection was not fresh=True: {fresh_flags}"
    finally:
        await _stop(task)


# -- presence index: atomic write, self-stop removal ---------------------------


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
    )
    key = bus._settings.presence_key(bus.identity.name)

    await bus._set_presence(recording, key, presence)

    # The SET and the index SADD ride ONE pipeline, in that order — a presence key never
    # exists without its index member.
    assert len(recording.pipelines) == 1
    assert recording.pipelines[0].commands == ["set", "sadd"]
    assert bus.identity.name in await inner.smembers(bus._settings.presence_index)
    assert await inner.get(key) is not None
    await inner.aclose()


async def test_deliberate_stop_removes_the_index_member_and_presence_key(wire_bus_client: None, server) -> None:
    bus = make_bus(kind=WorkerKind.serve)
    task, ident = await _spawn_subscriber(bus, _noop_op)

    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    assert ident.name in await client.smembers(bus._settings.presence_index)

    await _stop(task)

    # A deliberate stop releases the claim then removes BOTH the presence key and its
    # index member (the teardown mirror of the atomic write).
    assert ident.name not in await client.smembers(bus._settings.presence_index)
    assert await client.get(bus._settings.presence_key(ident.name)) is None
    await client.aclose()
