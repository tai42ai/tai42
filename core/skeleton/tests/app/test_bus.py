"""Worker bus unit/integration tests.

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
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import fakeredis
import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_contract.errors import ClientDisconnectedError

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app.bus import (
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    UnknownFleetTargetsError,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
    WorkerState,
    _decode,
    _merge_terminal,
    _PresenceValue,
    presence_fresh,
)
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


# -- census -------------------------------------------------------------------


async def test_census_lists_registered_workers(wire_bus_client: None) -> None:
    serve_bus = make_bus(kind=WorkerKind.serve)
    backend_bus = make_bus(kind=WorkerKind.backend)

    async def noop(_op: dict) -> None:
        return None

    t1, serve_id = await _spawn_subscriber(serve_bus, noop)
    t2, backend_id = await _spawn_subscriber(backend_bus, noop)
    try:
        census = await serve_bus.census()
        by_name = {row.name: row for row in census}
        assert set(by_name) == {serve_id.name, backend_id.name}
        assert by_name[serve_id.name].kind == WorkerKind.serve
        assert by_name[backend_id.name].kind == WorkerKind.backend
        # The slot names are the lowest free ordinals of their kind.
        assert serve_id.name == "serve-1"
        assert backend_id.name == "backend-1"
        # The presence value carries pid + generation so consumers tell procs/lives apart.
        assert by_name[backend_id.name].pid == backend_id.pid
        assert by_name[backend_id.name].generation == 1
    finally:
        await _stop(t1, t2)


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


# -- slot claim / generation / ownership (compare-token) ----------------------


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


def test_decode_rejects_non_json_and_non_object_frames() -> None:
    # A malformed wire frame is discarded (logged), never applied.
    assert _decode("this is not json") is None
    assert _decode(json.dumps(["not", "an", "object"])) is None
    assert _decode(json.dumps({"op": "x"})) == {"op": "x"}


def test_identity_raises_before_the_slot_is_claimed() -> None:
    # A real bus has no identity until it claims a slot at subscribe time; reading it
    # before then raises loudly rather than emitting a placeholder name.
    bus = make_bus()
    bus._identity = None
    with pytest.raises(RuntimeError, match="not minted"):
        _ = bus.identity


async def test_publish_requires_a_non_empty_op_name() -> None:
    bus = make_bus()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(ValueError, match="non-empty 'op' name"):
        await bus.publish({}, targets=None, local=local)


def test_presence_fresh_is_the_sole_home_of_the_bound() -> None:
    ttl = 15.0  # bound = ttl - 2*(ttl/3) = ttl/3 = 5s -> 5000ms
    assert presence_fresh(6000, ttl) is True
    assert presence_fresh(5000, ttl) is False  # AT the bound is not fresh (strictly above)
    assert presence_fresh(4000, ttl) is False
    assert presence_fresh(None, ttl) is False


# -- two-phase ack then apply -------------------------------------------------


async def test_two_phase_ack_then_slow_apply_reports_applied(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker = make_bus()
    seen: list[dict] = []

    async def slow_apply(op: dict) -> dict:
        seen.append(op)
        # Longer than the ack timeout, shorter than the apply timeout: the fast
        # received-ack must keep this worker from being judged missing.
        await asyncio.sleep(0.12)
        return {"reloaded": True}

    task, worker_id = await _spawn_subscriber(worker, slow_apply)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        assert result.ok
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.applied
        assert by_name[worker_id.name].payload == {"reloaded": True}
        assert by_name[publisher.identity.name].outcome == OpOutcome.applied
        assert seen == [{"op": "reload_config"}]
    finally:
        await _stop(task)


async def test_all_terminal_early_exit_returns_before_apply_deadline(wire_bus_client: None) -> None:
    publisher = make_bus(apply_timeout=5.0)
    worker = make_bus(apply_timeout=5.0)

    async def fast(_op: dict) -> None:
        return None

    task, _ = await _spawn_subscriber(worker, fast)
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        elapsed = loop.time() - start
        assert result.ok
        assert elapsed < 2.0
    finally:
        await _stop(task)


# -- discard gates (generation / op_id) + echo-skip ---------------------------


class _FramePubsub:
    """A pubsub that yields a fixed queue of pre-built reply frames, then None — so
    the collect gates can be driven with crafted (name, generation, op_id) frames."""

    def __init__(self, frames: list) -> None:
        self._frames = list(frames)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> dict | None:
        if self._frames:
            return {"data": json.dumps(self._frames.pop(0))}
        return None


async def test_collect_discards_stale_generation_and_opid(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        expected: dict[str, int | None] = {"serve-1": 5}
        frames = [
            # A malformed (non-object) frame is discarded.
            ["not", "an", "object"],
            # A reply from a worker not in the expected set is ignored.
            {"name": "serve-2", "generation": 1, "op_id": "op-A", "phase": "terminal", "outcome": "applied"},
            # Stale life (generation 4 != expected 5): discarded + warned. Carries a
            # DISTINCT worst-outcome (failed) from the true reply so that if the gate
            # regressed and folded this frame, worst-outcome-wins would flip the
            # collected result to failed and the outcome assertion below would fail.
            {"name": "serve-1", "generation": 4, "op_id": "op-A", "phase": "terminal", "outcome": "failed"},
            # Right life, wrong op: op_id mismatch discarded + warned. Also carries the
            # distinct failed outcome, so a folded op_id-mismatch frame would flip too.
            {"name": "serve-1", "generation": 5, "op_id": "op-B", "phase": "terminal", "outcome": "failed"},
            # The true reply is accepted.
            {"name": "serve-1", "generation": 5, "op_id": "op-A", "phase": "terminal", "outcome": "applied"},
        ]
        with caplog.at_level(logging.WARNING):
            results = await bus._collect(client, _FramePubsub(frames), expected, "op-A", "reload_config")
        # The discarded frames never fold, so the true reply stands: applied.
        assert results["serve-1"].outcome == OpOutcome.applied
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("expected generation 5" in m for m in messages)
        assert any("op_id mismatch" in m for m in messages)
    finally:
        await client.aclose()


class _RaisingPubsub:
    """A pubsub whose poll raises a transport error, modelling a blip mid-collection."""

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> dict | None:
        raise RedisConnectionError("reply collection blip")


async def test_collect_transport_error_is_reported_on_the_affected_worker(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A transport blip mid-collection is reported loudly on the affected worker (its
    # verdict carries the transport-error detail), never silently dropped.
    bus = make_bus(ack_timeout=0.03, apply_timeout=0.2)
    bus._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    # A live presence key so the finalize presence re-check finds it alive → missing.
    await _register_bare_presence(server, bus._settings, "serve-1", WorkerKind.serve, ttl_ms=None)
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        results = await bus._collect(client, _RaisingPubsub(), {"serve-1": 1}, "op-A", "reload_config")
        assert results["serve-1"].outcome == OpOutcome.missing
        assert "transport error" in (results["serve-1"].detail or "")
    finally:
        await client.aclose()


async def test_echo_skip_is_keyed_on_name_and_generation() -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=3)
    applied: list[dict] = []

    async def record(op: dict) -> None:
        applied.append(op)

    def frame(name: str, generation: int) -> dict:
        return {"data": json.dumps({"op": "reload_config", "name": name, "generation": generation})}

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


# -- missing vs departed (real TTL expiry) ------------------------------------


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
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    now = "2026-01-01T00:00:00+00:00"
    value = json.dumps(
        {"kind": kind.value, "pid": 4, "generation": 1, "joined_at": now, "beat_at": now, "state": state.value}
    )
    if ttl_ms is None:
        await client.set(settings.presence_key(name), value)
    else:
        await client.set(settings.presence_key(name), value, px=ttl_ms)
    await client.aclose()


async def test_silent_but_present_worker_is_missing(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    # Presence stays live throughout — no TTL — but nobody replies.
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=None)

    result = await publisher.publish({"op": "reload_config"}, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.missing
    assert by_name["backend-1"].detail is not None


async def test_targeted_name_absent_from_census_is_departed(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A targeted name with NO presence key at all stays in the expected set (generation
    # unknown) and is reported ``departed`` at the cut — never silently dropped.
    publisher = make_bus(ack_timeout=0.03, apply_timeout=0.1)
    result = await publisher.publish({"op": "reload_config"}, targets=["backend-9"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-9"].outcome == OpOutcome.departed


async def test_expired_presence_worker_is_departed(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus(ack_timeout=0.03, apply_timeout=0.25)
    # Alive when the census runs, expired by the cut — a genuine TTL expiry.
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=80)

    result = await publisher.publish({"op": "reload_config"}, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.departed
    assert by_name["backend-1"].detail is not None


# -- whole-fleet freshness/state gate (excludes non-ready or stale rows) -------


async def test_whole_fleet_publish_excludes_a_resyncing_row(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A resyncing row is on the census but NOT ready, so a whole-fleet op excludes it:
    # it is never counted expected and never reported missing/timed_out. Its PTTL is
    # fresh, so ONLY the non-ready state drives the exclusion.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.resyncing
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)

    names = {r.name for r in result.results}
    assert "backend-1" not in names  # excluded by the state gate — no phantom verdict
    assert names == {publisher.identity.name}  # only the publisher's own self entry


async def test_whole_fleet_publish_excludes_a_ready_but_decayed_row(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A READY row whose remaining PTTL is below the freshness bound is excluded from a
    # whole-fleet op: ready alone is not enough, the presence must also be fresh. With
    # heartbeat_ttl=5.0 the bound is ttl/3 ≈ 1666ms, so a 1000ms PTTL fails it.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=1000, state=WorkerState.ready
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)

    names = {r.name for r in result.results}
    assert "backend-1" not in names  # excluded by the freshness gate
    assert names == {publisher.identity.name}


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


# -- a non-member (fork child) is never a fleet target ------------------------


def _make_nonmember_bus(kind: WorkerKind = WorkerKind.serve) -> WorkerBus:
    """A bus re-derived to a fork child's NON-MEMBER identity: {parent}/fork-{pid},
    generation 0, member False — it claims nothing and registers no presence."""
    bus = make_bus(kind=kind)
    parent = bus.identity
    bus._identity = WorkerIdentity(
        name=f"{parent.name}/fork-{os.getpid()}", kind=kind, pid=os.getpid(), generation=0, member=False
    )
    return bus


async def test_nonmember_whole_fleet_publish_broadcasts_with_no_phantom_self(wire_bus_client: None) -> None:
    # A non-member publishing the whole fleet (targets=None, local=None) is NOT itself a
    # target: it broadcasts to the fleet and synthesizes NO self entry (a member would
    # be self-targeted and required to pass local).
    child = _make_nonmember_bus()
    member = make_bus()
    applied: list[dict] = []

    async def record(op: dict) -> None:
        applied.append(op)

    task, member_id = await _spawn_subscriber(member, record)
    try:
        result = await child.publish({"op": "reload_config"}, targets=None, local=None)
        names = {r.name for r in result.results}
        assert member_id.name in names  # the member applied the child's broadcast
        assert result.results[0].outcome == OpOutcome.applied
        assert child.identity.name not in names  # no phantom self entry for the non-member
        await _until(lambda: applied == [{"op": "reload_config"}])
    finally:
        await _stop(task)


async def test_nonmember_publish_with_local_raises(wire_bus_client: None) -> None:
    # A non-member is not a target, so supplying a local self result would name a self
    # entry for a non-target — a false report; the bidirectional gate raises.
    child = _make_nonmember_bus()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(ValueError, match="exclude the publisher"):
        await child.publish({"op": "reload_config"}, targets=None, local=local)


async def test_nonmember_validate_targets_rejects_its_own_name(wire_bus_client: None) -> None:
    # A non-member's own name is not on the census, so naming it as a target is unknown —
    # self is seeded into the live set only for a member.
    child = _make_nonmember_bus()
    with pytest.raises(UnknownFleetTargetsError):
        await child.validate_targets([child.identity.name])


async def test_member_validate_targets_accepts_its_own_name(wire_bus_client: None) -> None:
    # The member path is unchanged: a member seeds self into the live set, so naming
    # itself is always valid even before any sibling has registered.
    member = make_bus()
    await member.validate_targets([member.identity.name])  # must not raise


# -- echo-skip + self-confirmation --------------------------------------------


async def test_echo_skip_synthesizes_self_and_never_reapplies(wire_bus_client: None) -> None:
    # The publisher is ALSO subscribed (its own presence key in the census). Without
    # echo-skip it would wait for its own reply and report itself timed_out.
    bus = make_bus()
    applied: list[dict] = []

    async def record(op: dict) -> None:
        applied.append(op)

    task, self_id = await _spawn_subscriber(bus, record)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied, payload={"n": 1})
        result = await bus.publish({"op": "reload_config"}, targets=None, local=local)
        assert result.ok
        assert [r.name for r in result.results] == [self_id.name]
        assert result.results[0].outcome == OpOutcome.applied
        assert result.results[0].payload == {"n": 1}
        await asyncio.sleep(0.05)
        assert applied == []  # publisher never re-applies its own broadcast
    finally:
        await _stop(task)


async def test_self_failure_reported_when_local_failed(wire_bus_client: None) -> None:
    publisher = make_bus()
    local = LocalApplyResult(outcome=OpOutcome.failed, error="ValueError: boom")
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
    assert not result.ok
    self_entry = result.results[0]
    assert self_entry.outcome == OpOutcome.failed
    assert self_entry.error == "ValueError: boom"


# -- targets-vs-self bidirectional validation ---------------------------------


async def test_publish_raises_when_targeted_self_without_local(wire_bus_client: None) -> None:
    bus = make_bus()
    # A real bus names itself from its claimed identity; set one for the validation path.
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    with pytest.raises(ValueError, match="can never reply"):
        await bus.publish({"op": "reload_config"}, targets=None, local=None)
    with pytest.raises(ValueError, match="can never reply"):
        await bus.publish({"op": "reload_config"}, targets=[bus.identity.name], local=None)


async def test_publish_raises_when_local_given_but_self_excluded(wire_bus_client: None) -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(ValueError, match="exclude the publisher"):
        await bus.publish({"op": "reload_config"}, targets=["backend-1"], local=local)


# -- namespace isolation ------------------------------------------------------


async def test_namespace_isolation_no_cross_talk(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    bus_a = make_bus(namespace="stack-a")
    bus_b = make_bus(namespace="stack-b")
    a_calls: list[dict] = []

    async def record(op: dict) -> None:
        a_calls.append(op)

    task_a, id_a = await _spawn_subscriber(bus_a, record)
    try:
        # Presence keys are namespaced: A sees its worker, B sees an empty fleet.
        assert {row.name for row in await bus_a.census()} == {id_a.name}
        assert await bus_b.census() == []

        # B publishes on its own channel; A's subscriber (different channel) must not fire.
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result_b = await bus_b.publish({"op": "reload_config"}, targets=None, local=local)
        assert [r.name for r in result_b.results] == [bus_b.identity.name]
        await asyncio.sleep(0.05)
        assert a_calls == []  # no cross-channel delivery
    finally:
        await _stop(task_a)


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


async def _noop_op(_op: dict) -> None:
    return None


# -- worst-outcome-wins terminal merge ----------------------------------------


def test_merge_terminal_failure_is_never_overridden() -> None:
    applied = WorkerResult(name="serve-1", outcome=OpOutcome.applied)
    failed = WorkerResult(name="serve-1", outcome=OpOutcome.failed, error="boom")

    terminal: dict[str, WorkerResult] = {}
    _merge_terminal(terminal, "serve-1", applied)
    _merge_terminal(terminal, "serve-1", failed)
    assert terminal["serve-1"].outcome == OpOutcome.failed

    terminal = {}
    _merge_terminal(terminal, "serve-1", failed)
    _merge_terminal(terminal, "serve-1", applied)
    assert terminal["serve-1"].outcome == OpOutcome.failed


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


# -- bus-unreachable shape ----------------------------------------------------


async def test_bus_unreachable_shape(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    publisher._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    server.connected = False
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
    assert result.reachable is False
    assert result.results == []
    assert result.error is not None


async def test_bus_unreachable_shape_on_wrapped_disconnect(
    wire_pooled_bus_client: None, server: fakeredis.FakeServer
) -> None:
    publisher = make_bus()
    publisher._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    server.connected = False
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
    assert result.reachable is False
    assert result.results == []
    assert result.error is not None
    assert "ClientDisconnectedError" in result.error


# -- validate_targets ---------------------------------------------------------


async def test_validate_targets_raises_on_unknown(wire_bus_client: None) -> None:
    bus = make_bus()
    worker = make_bus()

    async def noop(_op: dict) -> None:
        return None

    _, self_id = await _spawn_subscriber(bus, noop)
    task, worker_id = await _spawn_subscriber(worker, noop)
    try:
        await bus.validate_targets(None)  # whole-fleet is always valid
        await bus.validate_targets([worker_id.name])
        await bus.validate_targets([self_id.name])
        with pytest.raises(UnknownFleetTargetsError, match="unknown fleet targets"):
            await bus.validate_targets(["backend-does-not-exist"])
    finally:
        await _stop(task)


# -- no-op local() variant ----------------------------------------------------


async def test_local_variant(wire_bus_client: None) -> None:
    bus = WorkerBus.local()

    local = LocalApplyResult(outcome=OpOutcome.applied, payload={"ok": True})
    result = await bus.publish({"op": "reload_config"}, targets=None, local=local)
    assert isinstance(result, FleetResult)
    assert result.local_only
    assert result.ok
    assert [r.name for r in result.results] == [bus.identity.name]
    assert result.results[0].payload == {"ok": True}

    # census: one synthesized ready row, beat_at computed at call time, no stale field.
    rows = await bus.census()
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "serve-1"
    assert row.generation == 1
    assert row.state == WorkerState.ready
    assert row.joined_at
    assert row.beat_at
    dumped = row.model_dump()
    assert "stale" not in dumped  # stale lives at the API layer only
    assert "pttl_ms" not in dumped  # the freshness measurement never leaks

    await bus.validate_targets([bus.identity.name])
    with pytest.raises(ValueError, match="cannot reach"):
        await bus.publish({"op": "x"}, targets=["backend-other"], local=None)
    with pytest.raises(ValueError, match="can never reply"):
        await bus.publish({"op": "x"}, targets=None, local=None)

    async def noop(_op: dict) -> None:
        return None

    task = asyncio.create_task(bus.subscribe(noop))
    await asyncio.sleep(0.05)
    assert not task.done()
    await _stop(task)


# -- timed_out: live worker, apply outlasts the report cut --------------------


async def test_live_but_slow_apply_is_timed_out(wire_bus_client: None) -> None:
    publisher = make_bus(ack_timeout=0.05, apply_timeout=0.5, heartbeat_ttl=0.5)
    worker = make_bus(ack_timeout=0.05, apply_timeout=0.5, heartbeat_ttl=0.5)

    async def slow(_op: dict) -> None:
        await asyncio.sleep(0.9)

    task, worker_id = await _spawn_subscriber(worker, slow)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.timed_out
        assert by_name[worker_id.name].detail is not None
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
