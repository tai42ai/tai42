"""Publisher-side behaviour: broadcast, reply collection + computed verdicts, the
census, ``expected_at_start`` re-admission, and target/self validation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import fakeredis
import pytest
from fakeredis import aioredis
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from tai42_kit.clients.impl.redis import KitAsyncRedis, RedisClient

import tai42_skeleton.app.bus as bus_module
from tai42_skeleton.app.bus import (
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    UnknownFleetTargetsError,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerState,
)
from tai42_skeleton.app.bus.ops import DeploymentKeysChanged, ReloadConfig
from tai42_skeleton.app.bus.replies import _ReplyLink
from tai42_skeleton.config.deployment_env import published_deployment_keys, published_env_readers

from ..._fakes.kit_redis import KitFakeRedis
from ..._helpers import another_skeleton_major, recapture_deployment_env
from .conftest import (
    _drop_presence,
    _end_life,
    _FramePubsub,
    _make_nonmember_bus,
    _RaisingPubsub,
    _RecordingRedis,
    _register_bare_presence,
    _spawn_subscriber,
    _stop,
    _until,
    make_bus,
)

_RELOAD = ReloadConfig(op="reload_config")
_RELOAD_WIRE = {"op": "reload_config"}

# -- census -------------------------------------------------------------------


async def test_census_lists_registered_workers(wire_bus_client: None) -> None:
    serve_bus = make_bus(kind=WorkerKind.serve)
    backend_bus = make_bus(kind=WorkerKind.backend)

    async def noop(_op: dict, *, deadline: float) -> None:
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


async def test_a_presence_row_carries_the_names_its_deployment_sets(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each worker publishes the names its deployment sets (names only, never values), and the
    # census reads them back, so a sibling judges ownership by the whole fleet.
    monkeypatch.setenv("BUS_PRESENCE_TEST_DEPLOYED", "deployment-value")
    recapture_deployment_env()
    bus = make_bus(kind=WorkerKind.backend)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        rows = {row.name: row for row in await make_bus().census()}
        assert "BUS_PRESENCE_TEST_DEPLOYED" in rows[identity.name].deployment_keys
        assert rows[identity.name].deployment_keys == list(published_deployment_keys())
        assert "deployment-value" not in rows[identity.name].model_dump_json()
        assert rows[identity.name].env_readers == published_env_readers().digest
    finally:
        await _stop(task)


async def test_the_bus_answers_the_env_reader_bodies_it_holds_in_one_read(wire_bus_client: None) -> None:
    # A worker's presence write leaves its reader description on the bus under the digest its
    # row names; a reader of the census fetches the bodies it lacks by digest, and a digest the
    # bus holds nothing under is left out of the answer.
    bus = make_bus(kind=WorkerKind.backend)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task, identity = await _spawn_subscriber(bus, noop)
    try:
        reader = make_bus()
        digest = {row.name: row for row in await reader.census()}[identity.name].env_readers
        missing = "0" * 64

        assert await reader.env_readers([digest, missing]) == {digest: published_env_readers().readers}
        assert await reader.env_readers([]) == {}
    finally:
        await _stop(task)


async def test_census_drops_and_prunes_a_member_whose_key_expired(wire_bus_client: None, server) -> None:
    bus = make_bus()
    settings = bus._settings
    await _register_bare_presence(server, settings, "serve-1", WorkerKind.serve, ttl_ms=None)
    await _register_bare_presence(server, settings, "serve-2", WorkerKind.serve, ttl_ms=None)
    # serve-2's presence key fades (its TTL lapses) while its index member remains — the
    # census must drop it from the rows AND self-heal the index by removing the member.
    await _drop_presence(server, settings, "serve-2")

    rows = await bus.census()
    assert {row.name for row in rows} == {"serve-1"}

    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    members = await client.smembers(settings.presence_index)
    await client.aclose()
    assert members == {"serve-1"}


async def test_census_empties_an_index_of_only_stale_members(wire_bus_client: None, server) -> None:
    bus = make_bus()
    settings = bus._settings
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    # Index members whose presence keys never existed (all faded) — the census returns
    # nothing and prunes the whole index.
    await client.sadd(settings.presence_index, "serve-1", "serve-2")

    rows = await bus.census()
    assert rows == []
    assert await client.smembers(settings.presence_index) == set()
    await client.aclose()


async def test_stale_prune_spares_a_name_re_minted_live_before_the_prune(
    wire_bus_client: None, server, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tai42_skeleton.app.bus.publish as publish_mod

    bus = make_bus()
    settings = bus._settings
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    # "serve-1" is in the index but has no live presence key at read time, so the census
    # GET returns None and marks it stale.
    await client.sadd(settings.presence_index, "serve-1")

    real_eval = publish_mod.eval_script

    async def racing_eval(r, script, numkeys, *args):
        # A re-mint claims the same slot name and writes its key + index member (the
        # atomic SET+SADD) AFTER the census GET saw None but BEFORE the prune runs.
        await _register_bare_presence(server, settings, "serve-1", WorkerKind.serve, ttl_ms=5000)
        return await real_eval(r, script, numkeys, *args)

    monkeypatch.setattr(publish_mod, "eval_script", racing_eval)

    rows = await bus.census()
    # The census snapshot saw None, so the re-minted worker is not in these rows...
    assert rows == []
    # ...but the conditional prune re-checks the key server-side and must NOT remove the
    # now-live member: the next census will see it.
    assert "serve-1" in await client.smembers(settings.presence_index)
    assert await client.get(settings.presence_key("serve-1")) is not None
    await client.aclose()


async def test_the_census_reads_only_the_rows_of_workers_of_its_own_major(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A worker of another skeleton major on the same Redis and namespace writes a presence row
    # of its own shape under its own keys: here a row with none of the deployment fields this
    # major's row carries. The census of this major reads this major's rows alone.
    with another_skeleton_major(monkeypatch):
        foreign = make_bus()._settings
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    now = "2026-01-01T00:00:00+00:00"
    foreign_row = {"kind": "serve", "pid": 7, "generation": 1, "joined_at": now, "beat_at": now, "state": "ready"}
    await client.set(foreign.presence_key("serve-1"), json.dumps(foreign_row), px=5000)
    await client.sadd(foreign.presence_index, "serve-1")
    await client.aclose()
    bus = make_bus()
    await _register_bare_presence(server, bus._settings, "serve-2", WorkerKind.serve, ttl_ms=5000)

    assert [row.name for row in await bus.census()] == ["serve-2"]


async def test_census_reads_the_index_never_a_keyspace_scan(recording_bus_client, server) -> None:
    bus = make_bus()
    settings = bus._settings
    await _register_bare_presence(server, settings, "serve-1", WorkerKind.serve, ttl_ms=5000)

    await bus.census()
    await bus.expected_at_start()
    await bus.validate_targets(["serve-1"])
    async with bus_module.client_ctx(RedisClient, settings.redis) as r:
        await bus._classify_workers(r, None)

    touched: set[str] = set().union(*(client.touched for client in recording_bus_client))
    assert not (touched & {"scan_iter", "scan", "keys"})
    assert "smembers" in touched


# -- two-phase ack then apply -------------------------------------------------


async def test_two_phase_ack_then_slow_apply_reports_applied(wire_bus_client: None) -> None:
    publisher = make_bus()
    worker = make_bus()
    seen: list[dict] = []

    async def slow_apply(op: dict, *, deadline: float) -> dict:
        seen.append(op)
        # A slow apply inside the apply timeout: the worker acks at once, is awaited to its
        # terminal reply, and lands applied.
        await asyncio.sleep(0.12)
        return {"reloaded": True}

    task, worker_id = await _spawn_subscriber(worker, slow_apply)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        assert result.ok
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.applied
        assert by_name[worker_id.name].payload == {"reloaded": True}
        assert by_name[publisher.identity.name].outcome == OpOutcome.applied
        assert seen == [_RELOAD_WIRE]
    finally:
        await _stop(task)


async def test_the_reply_collection_holds_a_dedicated_connection_outside_the_epoch_pool(
    monkeypatch: pytest.MonkeyPatch, server: fakeredis.FakeServer
) -> None:
    # The publish holds its connection across the reply collection: a pooled, epoch-stamped
    # lease would hold a reload's retire for the drain budget, so it is a fresh connection.
    opened: list[tuple[bool, _RecordingRedis]] = []

    @contextlib.asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        inner = KitFakeRedis(server=server, decode_responses=True)
        rec = _RecordingRedis(inner)
        opened.append((fresh, rec))
        try:
            yield rec
        finally:
            await inner.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)
    publisher = make_bus()
    await publisher._establish_identity(KitFakeRedis(server=server, decode_responses=True))

    await publisher.publish(_RELOAD, targets=None, local=LocalApplyResult(outcome=OpOutcome.applied))

    collecting = [fresh for fresh, rec in opened if "pubsub" in rec.touched]
    assert collecting == [True]


async def test_all_terminal_early_exit_returns_before_apply_deadline(wire_bus_client: None) -> None:
    publisher = make_bus(apply_timeout=5.0)
    worker = make_bus(apply_timeout=5.0)

    async def fast(_op: dict, *, deadline: float) -> None:
        return None

    task, _ = await _spawn_subscriber(worker, fast)
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        elapsed = loop.time() - start
        assert result.ok
        assert elapsed < 2.0
    finally:
        await _stop(task)


# -- discard gates (generation / op_id) ---------------------------------------


async def test_collect_discards_stale_generation_and_opid(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        expected: dict[str, int] = {"serve-1": 5}
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
            results = await bus._collect(
                client, _FramePubsub(frames), _ReplyLink(), expected, {}, set(), "op-A", "reload_config"
            )
        # The discarded frames never fold, so the true reply stands: applied.
        assert results["serve-1"].outcome == OpOutcome.applied
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("expected generation 5" in m for m in messages)
        assert any("op_id mismatch" in m for m in messages)
    finally:
        await client.aclose()


async def test_collect_transport_error_is_reported_on_the_affected_worker(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A transport blip mid-collection is reported loudly on the affected worker (its
    # verdict carries the transport-error detail), never silently dropped.
    bus = make_bus(apply_timeout=0.2)
    bus._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    # A held slot claim so the report-cut re-read finds the life alive → missing.
    await _register_bare_presence(server, bus._settings, "serve-1", WorkerKind.serve, ttl_ms=None)
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        results = await bus._collect(
            client, _RaisingPubsub(), _ReplyLink(), {"serve-1": 1}, {}, set(), "op-A", "reload_config"
        )
        assert results["serve-1"].outcome == OpOutcome.missing
        assert "transport error" in (results["serve-1"].detail or "")
    finally:
        await client.aclose()


async def test_a_reply_lost_to_a_reconnect_the_retry_hides_names_the_reconnect(
    wire_bus_client: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The publisher's reply connection drops while the worker applies, so the worker's
    # terminal reply is published while it is down. The next read reconnects and
    # re-subscribes inside the call, so no error reaches the collector; the verdict
    # names that reconnect as the cause of the lost reply.
    pubsubs: list[Any] = []
    kit_pubsub = KitAsyncRedis.pubsub

    def recording_pubsub(self: KitAsyncRedis, **kwargs: Any) -> Any:
        pubsub = kit_pubsub(self, **kwargs)
        pubsubs.append(pubsub)
        return pubsub

    monkeypatch.setattr(KitFakeRedis, "pubsub", recording_pubsub)
    publisher = make_bus(apply_timeout=0.5)

    async def apply(op: dict, *, deadline: float) -> None:
        return None

    worker = make_bus()
    send_reply = worker._reply

    async def reply_while_the_publisher_is_down(r: Any, channel: str, frame: dict[str, Any]) -> None:
        if frame["phase"] != "terminal":
            await send_reply(r, channel, frame)
            return
        # Drop the publisher's reply connection (the last pub/sub built) as the server
        # sees a lost link, and hold its reconnect until the terminal reply is out.
        connection = pubsubs[-1].connection
        replied = asyncio.Event()
        connect = connection.connect

        async def connect_after_the_reply() -> None:
            await replied.wait()
            await connect()

        monkeypatch.setattr(connection, "connect", connect_after_the_reply)
        connection._sock.kill()
        await connection.disconnect()
        await send_reply(r, channel, frame)
        replied.set()

    monkeypatch.setattr(worker, "_reply", reply_while_the_publisher_is_down)
    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        caplog.set_level(logging.WARNING, logger="tai42_skeleton.app.bus.replies")
        result = await publisher.publish(_RELOAD, targets=None, local=LocalApplyResult(outcome=OpOutcome.applied))
        verdict = {r.name: r for r in result.results}[worker_id.name]
        # The received ack may also have been queued on the dropped connection, so the
        # worker reads as acked-and-stuck or as silent; either way the cause is named.
        assert verdict.outcome in {OpOutcome.timed_out, OpOutcome.missing}
        assert "reply subscription reconnected" in (verdict.detail or ""), verdict.detail
        assert [r for r in caplog.records if "reconnected" in r.getMessage() and r.levelno == logging.WARNING]
    finally:
        await _stop(task)


# -- missing vs departed (real TTL expiry) ------------------------------------


async def test_silent_but_present_worker_is_missing(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    # Ready + fresh at the census (a comfortably-above-bound TTL that stays live through
    # the whole short apply window), so it is an EXPECTED worker — yet nobody replies.
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)

    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.missing
    assert by_name["backend-1"].detail is not None


async def test_targeted_name_absent_from_census_is_departed(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A targeted name with NO presence key at all stays in the expected set (at the last
    # generation its slot minted) and is reported ``departed`` — never silently dropped.
    publisher = make_bus(apply_timeout=0.1)
    result = await publisher.publish(_RELOAD, targets=["backend-9"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-9"].outcome == OpOutcome.departed


async def test_a_worker_whose_claim_lapsed_is_departed(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target's presence is FRESH at the census (so it is expected), and its claim and row
    # TTLs run out right after the census, well before the apply cut (600ms) — a genuine TTL
    # expiry mid-apply, reported departed.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.6)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    classify = publisher._classify_workers
    censuses: list[dict[str, int]] = []

    async def _census_then_expire(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        censuses.append(censused[0])
        client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
        await client.pexpire(publisher._settings.slot_key("backend-1"), 1)
        await client.pexpire(publisher._settings.presence_key("backend-1"), 1)
        await client.aclose()
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", _census_then_expire)

    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None)
    assert censuses == [{"backend-1": 1}], "the target was not censused ready and fresh"
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.departed
    assert by_name["backend-1"].detail is not None


# -- a life that ends mid-collection is departed at once ------------------------
#
# The publisher re-reads each outstanding worker's slot claim and generation once per heartbeat
# interval while it collects. A life whose claim is gone, or whose slot a newer life holds, can
# never reply, so it is reported ``departed`` then instead of being waited on to the apply deadline.

_LONG_APPLY = 10.0


def _presence_value(generation: int) -> str:
    now = "2026-01-01T00:00:00+00:00"
    return json.dumps(
        {
            "kind": "backend",
            "pid": 9,
            "generation": generation,
            "joined_at": now,
            "beat_at": now,
            "state": "ready",
            "deployment_keys": [],
            "env_readers": published_env_readers().digest,
            "drain_seconds": None,
        }
    )


async def _take_the_slot(server: fakeredis.FakeServer, settings: Any, name: str) -> None:
    """A newer life takes the slot ``name``, as a claim does: its own claim, the next generation, its row."""
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    await client.set(settings.slot_key(name), f"claim-of-a-newer-{name}", px=5000)
    generation = int(await client.incr(settings.gen_key(name)))
    await client.set(settings.presence_key(name), _presence_value(generation), px=5000)
    await client.aclose()


async def _publish_timed(publisher: WorkerBus, targets: list[str] | None) -> tuple[FleetResult, float]:
    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await publisher.publish(_RELOAD, targets=targets, local=None)
    return result, loop.time() - start


async def test_a_life_that_ends_mid_collection_is_departed_at_once(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ready and fresh at the census, then the life ends (its stop releases the claim and deletes
    # the row) while the publisher collects: the life is over, so the publisher stops waiting on it.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    classify = publisher._classify_workers

    async def census_then_the_life_ends(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        await _end_life(server, publisher._settings, "backend-1")
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", census_then_the_life_ends)
    result, elapsed = await _publish_timed(publisher, ["backend-1"])
    row = {r.name: r for r in result.results}["backend-1"]
    assert row.outcome == OpOutcome.departed, row
    assert "holds no claim" in (row.detail or "")
    assert elapsed < 3.0, f"waited {elapsed:.1f}s on a life that ended"


async def test_a_life_that_acked_and_then_ended_is_departed_mid_apply_at_once(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # The worker acks the op, then its life ends (its stop releases the claim and deletes the
    # row) before it applies: the life read reports it departed mid-apply, not timed_out at the cut.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)
    publisher._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    ack = {"name": "backend-1", "generation": 1, "op_id": "op-A", "phase": "received"}

    class _AckThenTheLifeEnds:
        """A reply subscription that delivers the worker's ack, then ends its life on the next poll."""

        def __init__(self) -> None:
            self.acked = False
            self.ended = False

        async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> dict | None:
            if not self.acked:
                self.acked = True
                return {"data": json.dumps(ack)}
            if not self.ended:
                self.ended = True
                await _end_life(server, publisher._settings, "backend-1")
            await asyncio.sleep(timeout)
            return None

    client = aioredis.FakeRedis(server=server, decode_responses=True)
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        results = await publisher._collect(
            client, _AckThenTheLifeEnds(), _ReplyLink(), {"backend-1": 1}, {}, set(), "op-A", "reload_config"
        )
    finally:
        await client.aclose()
    elapsed = loop.time() - start
    row = results["backend-1"]
    assert row.outcome == OpOutcome.departed, row
    assert "acked, then its slot claim ended before it applied" in (row.detail or "")
    assert elapsed < 3.0, f"waited {elapsed:.1f}s on an acked life that ended"


async def test_a_life_whose_slot_a_newer_life_holds_is_departed_at_once(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Expected at generation 1; a new life takes the slot at generation 2 while the publisher
    # collects. The old life's replies can never come under the new slot holder, so it is departed.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)
    settings = publisher._settings
    await _register_bare_presence(server, settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    classify = publisher._classify_workers

    async def census_then_a_newer_life_takes_the_slot(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        await _take_the_slot(server, settings, "backend-1")
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", census_then_a_newer_life_takes_the_slot)
    result, elapsed = await _publish_timed(publisher, ["backend-1"])
    row = {r.name: r for r in result.results}["backend-1"]
    assert row.outcome == OpOutcome.departed, row
    assert "generation 2" in (row.detail or "")
    assert elapsed < 3.0, f"waited {elapsed:.1f}s on a life a newer life replaced"


async def test_a_life_whose_row_lapsed_while_its_slot_claim_holds_is_awaited(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The heartbeat renews the claim before it writes the row, so a beat whose row write failed
    # leaves the claim held after the row expired, and the life re-establishes itself on its next
    # connect and still applies. A missing row does not end the life: it stays awaited.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=0.4)
    settings = publisher._settings
    # The row's life holds its slot claim at generation 1 (see the helper).
    await _register_bare_presence(server, settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    classify = publisher._classify_workers

    async def census_then_the_row_lapses(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        await _drop_presence(server, settings, "backend-1")
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", census_then_the_row_lapses)
    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None)
    row = {r.name: r for r in result.results}["backend-1"]
    assert row.outcome == OpOutcome.missing, row
    assert "slot claim stayed held" in (row.detail or "")


# -- a life that begins leaving after publish -----------------------------------
#
# A worker expected ``ready`` whose stop begins while the op is out keeps its slot claim and its
# row, now reading ``recycling``, through its whole stop. The stop cancels the op it was applying,
# so no terminal reply comes; at the report cut it is reported as leaving, never as a stuck or
# silent worker.


async def test_an_acked_worker_that_begins_leaving_mid_apply_is_recycling_at_the_cut(
    wire_bus_client: None,
) -> None:
    publisher = make_bus(apply_timeout=0.5, heartbeat_ttl=5.0)
    worker = make_bus(apply_timeout=0.5, heartbeat_ttl=5.0)

    async def stop_begins_mid_apply(_op: dict, *, deadline: float) -> None:
        await worker.mark_recycling()
        await asyncio.Event().wait()

    task, identity = await _spawn_subscriber(worker, stop_begins_mid_apply)
    try:
        result = await publisher.publish(_RELOAD, targets=[identity.name], local=None)
        row = {r.name: r for r in result.results}[identity.name]
        assert row.outcome == OpOutcome.recycling, row
        assert row.detail == (
            "acked, then began leaving before it replied — it converges by ending this life, not by applying this op"
        )
    finally:
        await _stop(task)


async def test_a_silent_worker_that_begins_leaving_after_publish_is_recycling_at_the_cut(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.4)
    settings = publisher._settings
    await _register_bare_presence(server, settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    classify = publisher._classify_workers

    async def census_then_the_stop_begins(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        await _register_bare_presence(
            server, settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.recycling
        )
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", census_then_the_stop_begins)
    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None)
    row = {r.name: r for r in result.results}["backend-1"]
    assert row.outcome == OpOutcome.recycling, row
    assert row.detail == ("began leaving before it replied — it converges by ending this life, not by applying this op")


async def test_a_target_absent_at_publish_whose_slot_a_new_life_takes_is_departed_at_once(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target's life 1 left before the publish (no presence key), and life 2 takes the slot
    # right after the publish-time census. Life 2 claimed after the census, so the op is not
    # addressed to it: the life the publish addressed is gone, and it is departed at once.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)
    settings = publisher._settings
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    await client.set(settings.gen_key("backend-1"), 1)
    classify = publisher._classify_workers

    async def census_then_a_new_life_takes_the_slot(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        await client.incr(settings.gen_key("backend-1"))
        await client.set(settings.presence_key("backend-1"), _presence_value(2))
        await client.sadd(settings.presence_index, "backend-1")
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", census_then_a_new_life_takes_the_slot)
    try:
        result, elapsed = await _publish_timed(publisher, ["backend-1"])
    finally:
        await client.aclose()
    row = {r.name: r for r in result.results}["backend-1"]
    assert row.outcome == OpOutcome.departed, row
    assert "generation 2" in (row.detail or "")
    assert elapsed < 3.0, f"waited {elapsed:.1f}s on a target a life that never got the op replaced"


async def test_a_target_absent_at_publish_is_expected_at_the_last_life_its_slot_minted(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A life claims its slot after it subscribes and before it writes presence, so the last life
    # the slot minted is the newest one subscribed when the op goes out; a slot never minted
    # holds no life (generation 0), and any life that takes it later is a newer one.
    publisher = make_bus()
    async with bus_module.client_ctx(RedisClient, publisher._settings.redis) as r:
        await r.set(publisher._settings.gen_key("backend-1"), 3)
        assert await publisher._classify_workers(r, ["backend-1", "backend-2"]) == (
            {"backend-1": 3, "backend-2": 0},
            {},
            set(),
        )


async def test_a_live_worker_slower_than_many_life_reads_is_waited_on(wire_bus_client: None) -> None:
    # A live worker renews its slot claim on its own thread while it applies, so every life
    # read during the collection sees its claim held: it is waited on and answers applied.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)
    worker = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)

    async def slow(_op: dict, *, deadline: float) -> dict:
        # Ten life-read intervals of the publisher (heartbeat_ttl / 3 = 0.1 s).
        await asyncio.sleep(1.0)
        return {"reloaded": True}

    task, worker_id = await _spawn_subscriber(worker, slow)
    try:
        result, _elapsed = await _publish_timed(publisher, [worker_id.name])
        row = {r.name: r for r in result.results}[worker_id.name]
        assert row.outcome == OpOutcome.applied, row
        assert row.payload == {"reloaded": True}
    finally:
        await _stop(task)


async def test_a_reply_delivered_before_the_life_is_seen_gone_is_kept(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # The worker replied and then left: its reply sits delivered on the reply subscription when
    # the life read finds its slot claim gone. The delivered reply is the answer, not ``departed``.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=_LONG_APPLY)
    publisher._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    reply = {"name": "backend-1", "generation": 1, "op_id": "op-A", "phase": "terminal", "outcome": "applied"}

    class _DeliveredOnlyOnAReadThatDoesNotWait:
        """A reply subscription whose one reply is only read by a read that does not wait."""

        def __init__(self) -> None:
            self.frames = [reply]

        async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> dict | None:
            if timeout == 0 and self.frames:
                return {"data": json.dumps(self.frames.pop(0))}
            await asyncio.sleep(timeout)
            return None

    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        results = await publisher._collect(
            client,
            _DeliveredOnlyOnAReadThatDoesNotWait(),
            _ReplyLink(),
            {"backend-1": 1},
            {},
            set(),
            "op-A",
            "reload_config",
        )
    finally:
        await client.aclose()
    assert results["backend-1"].outcome == OpOutcome.applied


async def test_a_life_read_that_fails_mid_collection_keeps_waiting_and_logs(
    wire_bus_client: None, server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # A life read that cannot reach the bus proves nothing about the life: it is logged and
    # the worker stays awaited to the apply deadline, where the cut decides its verdict.
    publisher = make_bus(heartbeat_ttl=0.3, apply_timeout=0.5)
    publisher._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=None)
    client = aioredis.FakeRedis(server=server, decode_responses=True)

    class _PipelineDown:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def pipeline(self, transaction: bool = True) -> Any:
            raise RedisConnectionError("bus down")

    try:
        with caplog.at_level(logging.ERROR):
            results = await publisher._collect(
                _PipelineDown(client), _FramePubsub([]), _ReplyLink(), {"backend-1": 1}, {}, set(), "op-A", "reload"
            )
    finally:
        await client.aclose()
    assert results["backend-1"].outcome == OpOutcome.missing
    assert any(r.levelno == logging.ERROR and "life read" in r.getMessage() for r in caplog.records)


# -- whole-fleet gap outcomes (a row failing the ready+fresh gate is CLASSIFIED) ----


async def test_whole_fleet_publish_reports_a_resyncing_row(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A resyncing row is on the census but NOT ready, so it is not an expected worker; it
    # is landed as its ACTUAL condition (``resyncing``), never silently dropped and never
    # reported missing/timed_out. Its PTTL is fresh, so the written state drives it.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.resyncing
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.resyncing
    assert "resyncing" in (by_name["backend-1"].detail or "")


async def test_an_op_awaited_mid_resync_expects_a_fresh_resyncing_row(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A fresh resyncing worker is subscribed and applies a delivered op after its resync, so
    # an op that declares it owes a reply awaits it: never reported as the resyncing gap.
    # Nobody replies here, so the cut finds it alive, still resyncing, and says so.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.resyncing
    )
    op = DeploymentKeysChanged(op="deployment_keys_changed")

    result = await publisher.publish(op, targets=None, local=LocalApplyResult(outcome=OpOutcome.applied))

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.missing
    assert "resyncing at publish" in (by_name["backend-1"].detail or "")
    async with bus_module.client_ctx(RedisClient, publisher._settings.redis) as r:
        for targets in (None, ["backend-1"]):
            assert await publisher._classify_workers(r, targets, await_resyncing=True) == (
                {"backend-1": 1},
                {},
                {"backend-1"},
            )


async def test_whole_fleet_publish_reports_a_recycling_row(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A fresh recycling row is landed as ``recycling`` — it is departing and converges by
    # old-life-gone + fresh capacity, not by a reply.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.recycling
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.recycling


async def test_whole_fleet_publish_reports_a_ready_but_decayed_row_as_stale(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A READY row whose remaining PTTL is below the freshness bound is a gap row: ready
    # alone is not enough, the presence must also be fresh. It is landed as ``stale`` (a
    # quiet row, reconnecting or dead), carrying no convergence promise. With
    # heartbeat_ttl=5.0 the bound is ttl/3 ≈ 1666ms, so a 1000ms PTTL fails it.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=1000, state=WorkerState.ready
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.stale


async def test_whole_fleet_publish_reports_a_decayed_resyncing_row_as_stale(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A resyncing row whose PTTL has decayed maps to ``stale`` regardless of its written
    # state — a worker that died mid-resync must not carry a convergence promise.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=1000, state=WorkerState.resyncing
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.stale


async def test_targeted_gap_row_carries_its_gap_outcome(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A named target that is present-but-gap carries its gap outcome (not departed, not
    # awaited): a resyncing target is reported ``resyncing``.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.resyncing
    )

    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.resyncing


async def test_targeted_worker_replaced_mid_apply_is_departed(
    wire_bus_client: None, server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target is ready+fresh at publish (expected at generation 1) but nobody replies,
    # and right after the census a replacement takes the slot at a HIGHER generation. The
    # generation-aware verdict is ``departed``, naming the superseding generation, never
    # timed_out/missing.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.2)
    settings = publisher._settings
    await _register_bare_presence(server, settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    classify = publisher._classify_workers

    async def census_then_a_replacement_takes_the_slot(*args: Any, **kwargs: Any) -> Any:
        censused = await classify(*args, **kwargs)
        await _take_the_slot(server, settings, "backend-1")
        return censused

    monkeypatch.setattr(publisher, "_classify_workers", census_then_a_replacement_takes_the_slot)
    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.departed
    assert "generation 2" in (by_name["backend-1"].detail or "")


# -- expected_at_start: membership pinned to op start, not to publish time -----
#
# publish censuses when it is called, which for every caller that applies locally first
# is AFTER that apply. A worker whose presence fades across the apply is off that census
# entirely — neither expected nor a gap row — so the op would report converged without
# it. The caller hands in its own op-start census to close that window.


async def test_a_worker_that_fades_before_publish_is_off_the_census_entirely(wire_bus_client: None, server) -> None:
    # WHY the snapshot is load-bearing: with no ``expected_at_start``, a live worker whose
    # presence key is gone at publish time is not expected, not a gap row, and not in the
    # report — the op reads as fully converged while a worker never confirmed.
    publisher = make_bus(heartbeat_ttl=30.0, apply_timeout=0.4)
    worker = make_bus(heartbeat_ttl=30.0, apply_timeout=0.4)

    async def apply(_op: dict, *, deadline: float) -> None:
        return None

    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        await _drop_presence(server, publisher._settings, worker_id.name)
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        assert [r.name for r in result.results] == [publisher.identity.name]
        assert result.ok
    finally:
        await _stop(task)


async def test_expected_at_start_still_collects_a_faded_workers_confirmation(wire_bus_client: None, server) -> None:
    # The fixed window: ready+fresh at op start, presence gone by publish, worker still
    # alive and replying. Carried in at its snapshot generation, so its terminal reply
    # passes the generation gate and lands as ``applied`` — the confirmation is collected,
    # not guessed at.
    publisher = make_bus(heartbeat_ttl=30.0, apply_timeout=0.4)
    worker = make_bus(heartbeat_ttl=30.0, apply_timeout=0.4)

    async def apply(_op: dict, *, deadline: float) -> dict:
        return {"reloaded": True}

    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        # The op-start census the publisher would have taken before its local apply.
        at_start = {row.name: row.generation for row in await publisher.census() if row.name == worker_id.name}
        assert at_start == {worker_id.name: worker_id.generation}

        await _drop_presence(server, publisher._settings, worker_id.name)
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local, expected_at_start=at_start)

        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.applied
        assert by_name[worker_id.name].payload == {"reloaded": True}
        assert result.ok
    finally:
        await _stop(task)


async def test_expected_at_start_worker_that_never_returns_is_departed_at_the_cut(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # The edge the late census was incidentally hiding: a worker that legitimately went
    # away between op start and publish. Carrying it in cannot hang the collection — the
    # apply deadline bounds it — and the re-read slot claim makes the verdict honest:
    # ``departed``, which drops the report off ``ok`` so the publisher logs it loudly.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.2)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    at_start = {"backend-1": 1}
    await _end_life(server, publisher._settings, "backend-1")

    loop = asyncio.get_running_loop()
    start = loop.time()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local, expected_at_start=at_start)
    elapsed = loop.time() - start

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.departed
    assert "holds no claim" in (by_name["backend-1"].detail or "")
    assert not result.ok
    # Bounded by the apply deadline, never an open-ended wait on a worker that is gone.
    assert elapsed < 2.0


async def test_expected_at_start_never_overrides_a_live_gap_row(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A carried name the publish-time census DOES still see keeps that census's verdict.
    # A worker that has announced ``recycling`` is departing on purpose and converges by
    # old-life-gone plus fresh capacity — re-admitting it would await a reply it never
    # owed and burn the apply timeout on a planned exit.
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=5.0)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.recycling
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local, expected_at_start={"backend-1": 1})
    elapsed = loop.time() - start

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.recycling
    assert elapsed < 2.0


async def test_expected_at_start_never_widens_targets_nor_re_admits_self(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # Two names the snapshot must not smuggle into a targeted op's expected set: a worker
    # outside ``targets`` (the op does not concern it) and the publisher's own name (it is
    # reported from its own local result and could never reply to itself).
    publisher = make_bus(heartbeat_ttl=5.0, apply_timeout=0.2)
    at_start = {"backend-2": 1, publisher.identity.name: 1}

    result = await publisher.publish(_RELOAD, targets=["backend-1"], local=None, expected_at_start=at_start)

    assert [r.name for r in result.results] == ["backend-1"]


# -- a non-member (fork child) is never a fleet target ------------------------


async def test_nonmember_whole_fleet_publish_broadcasts_with_no_phantom_self(wire_bus_client: None) -> None:
    # A non-member publishing the whole fleet (targets=None, local=None) is NOT itself a
    # target: it broadcasts to the fleet and synthesizes NO self entry (a member would
    # be self-targeted and required to pass local).
    child = _make_nonmember_bus()
    member = make_bus()
    applied: list[dict] = []

    async def record(op: dict, *, deadline: float) -> None:
        applied.append(op)

    task, member_id = await _spawn_subscriber(member, record)
    try:
        result = await child.publish(_RELOAD, targets=None, local=None)
        names = {r.name for r in result.results}
        assert member_id.name in names  # the member applied the child's broadcast
        assert result.results[0].outcome == OpOutcome.applied
        assert child.identity.name not in names  # no phantom self entry for the non-member
        await _until(lambda: applied == [_RELOAD_WIRE])
    finally:
        await _stop(task)


async def test_nonmember_publish_with_local_raises(wire_bus_client: None) -> None:
    # A non-member is not a target, so supplying a local self result would name a self
    # entry for a non-target — a false report; the bidirectional gate raises.
    child = _make_nonmember_bus()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(ValueError, match="exclude the publisher"):
        await child.publish(_RELOAD, targets=None, local=local)


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

    async def record(op: dict, *, deadline: float) -> None:
        applied.append(op)

    task, self_id = await _spawn_subscriber(bus, record)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied, payload={"n": 1})
        result = await bus.publish(_RELOAD, targets=None, local=local)
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
    result = await publisher.publish(_RELOAD, targets=None, local=local)
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
        await bus.publish(_RELOAD, targets=None, local=None)
    with pytest.raises(ValueError, match="can never reply"):
        await bus.publish(_RELOAD, targets=[bus.identity.name], local=None)


async def test_publish_raises_when_local_given_but_self_excluded(wire_bus_client: None) -> None:
    bus = make_bus()
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(ValueError, match="exclude the publisher"):
        await bus.publish(_RELOAD, targets=["backend-1"], local=local)


# -- namespace isolation ------------------------------------------------------


async def test_namespace_isolation_no_cross_talk(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    bus_a = make_bus(namespace="stack-a")
    bus_b = make_bus(namespace="stack-b")
    a_calls: list[dict] = []

    async def record(op: dict, *, deadline: float) -> None:
        a_calls.append(op)

    task_a, id_a = await _spawn_subscriber(bus_a, record)
    try:
        # Presence keys are namespaced: A sees its worker, B sees an empty fleet.
        assert {row.name for row in await bus_a.census()} == {id_a.name}
        assert await bus_b.census() == []

        # B publishes on its own channel; A's subscriber (different channel) must not fire.
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result_b = await bus_b.publish(_RELOAD, targets=None, local=local)
        assert [r.name for r in result_b.results] == [bus_b.identity.name]
        await asyncio.sleep(0.05)
        assert a_calls == []  # no cross-channel delivery
    finally:
        await _stop(task_a)


# -- bus-unreachable shape ----------------------------------------------------


async def test_bus_unreachable_shape(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    publisher._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    server.connected = False
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(_RELOAD, targets=None, local=local)
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
    result = await publisher.publish(_RELOAD, targets=None, local=local)
    assert result.reachable is False
    assert result.results == []
    assert result.error is not None
    assert "ClientDisconnectedError" in result.error


# -- validate_targets ---------------------------------------------------------


async def test_validate_targets_raises_on_unknown(wire_bus_client: None) -> None:
    bus = make_bus()
    worker = make_bus()

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    self_task, self_id = await _spawn_subscriber(bus, noop)
    task, worker_id = await _spawn_subscriber(worker, noop)
    try:
        await bus.validate_targets(None)  # whole-fleet is always valid
        await bus.validate_targets([worker_id.name])
        await bus.validate_targets([self_id.name])
        with pytest.raises(UnknownFleetTargetsError, match="unknown fleet targets"):
            await bus.validate_targets(["backend-does-not-exist"])
    finally:
        await _stop(self_task, task)


# -- no-op local() variant ----------------------------------------------------


async def test_local_variant(wire_bus_client: None) -> None:
    bus = WorkerBus.local()

    local = LocalApplyResult(outcome=OpOutcome.applied, payload={"ok": True})
    result = await bus.publish(_RELOAD, targets=None, local=local)
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
    # With no bus the fleet is the process: its row carries the names this deployment sets.
    assert row.deployment_keys == list(published_deployment_keys())
    assert row.env_readers == published_env_readers().digest
    own = published_env_readers()
    assert await bus.env_readers([own.digest, "0" * 64]) == {own.digest: own.readers}

    await bus.validate_targets([bus.identity.name])
    with pytest.raises(ValueError, match="cannot reach"):
        await bus.publish(_RELOAD, targets=["backend-other"], local=None)
    with pytest.raises(ValueError, match="can never reply"):
        await bus.publish(_RELOAD, targets=None, local=None)

    async def noop(_op: dict, *, deadline: float) -> None:
        return None

    task = asyncio.create_task(bus.subscribe(noop))
    await asyncio.sleep(0.05)
    assert not task.done()
    await _stop(task)


# -- timed_out: live worker, apply outlasts the report cut --------------------


async def test_live_but_slow_apply_is_timed_out(wire_bus_client: None) -> None:
    publisher = make_bus(apply_timeout=0.5, heartbeat_ttl=0.5)
    worker = make_bus(apply_timeout=0.5, heartbeat_ttl=0.5)

    async def slow(_op: dict, *, deadline: float) -> None:
        await asyncio.sleep(0.9)

    task, worker_id = await _spawn_subscriber(worker, slow)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.timed_out
        assert by_name[worker_id.name].detail is not None
    finally:
        await _stop(task)


async def test_a_worker_draining_inside_the_drain_floor_answers_applied(wire_bus_client: None) -> None:
    # A window at the retire's drain floor covers a worker whose reload drains real work
    # for 2.5 drain budgets: it answers inside the window and is reported applied.
    from tai42_skeleton.app.epoch_retire import retire_drain_floor_seconds

    budget = 1.0
    window = retire_drain_floor_seconds(budget)
    publisher = make_bus(apply_timeout=window, heartbeat_ttl=5.0)
    worker = make_bus(apply_timeout=window, heartbeat_ttl=5.0)

    async def draining(_op: dict, *, deadline: float) -> None:
        await asyncio.sleep(2.5 * budget)

    task, worker_id = await _spawn_subscriber(worker, draining)
    try:
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish(_RELOAD, targets=None, local=local)
        by_name = {r.name: r for r in result.results}
        assert by_name[worker_id.name].outcome == OpOutcome.applied
        assert result.ok
    finally:
        await _stop(task)


# -- report-cut life re-read branches ----------------------------------------


async def test_a_life_re_read_that_fails_at_the_cut_degrades_to_loud_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A transport error raised by the report-cut re-read must not be swallowed into a silent
    # verdict: it logs an ERROR and degrades to the loud missing/timed_out classification,
    # never departed or dropped, and the row says the life could not be re-read.
    bus = make_bus()

    class _BoomPipeline:
        def pipeline(self, transaction: bool = True) -> Any:
            raise RedisConnectionError("bus down")

    with caplog.at_level(logging.ERROR):
        life = await bus._reread_life(_BoomPipeline(), "serve-2")
        never_acked = await bus._computed_verdict(_BoomPipeline(), "serve-2", 1, acked=False, transport_error=None)
        acked = await bus._computed_verdict(_BoomPipeline(), "serve-2", 1, acked=True, transport_error=None)

    assert life is None
    assert never_acked.outcome is OpOutcome.missing
    assert acked.outcome is OpOutcome.timed_out
    for row in (never_acked, acked):
        assert "could not be re-read" in (row.detail or ""), row
        assert "stayed held" not in (row.detail or ""), row
    assert any(r.levelno == logging.ERROR and "life re-read" in r.getMessage() for r in caplog.records), (
        "the transport-error degrade did not log loudly"
    )


async def test_a_row_that_is_not_a_presence_value_raises_at_the_cut(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # The report cut parses the row with the census's one parse: a value that is not a
    # presence value raises, never read as some guessed life.
    bus = make_bus()
    await _register_bare_presence(server, bus._settings, "serve-2", WorkerKind.serve, ttl_ms=5000)
    client: Any = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        await client.set(bus._settings.presence_key("serve-2"), "not-json", px=5000)
        with pytest.raises(ValidationError):
            await bus._reread_life(client, "serve-2")
    finally:
        await client.aclose()
