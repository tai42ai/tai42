"""Publisher-side behaviour: broadcast, reply collection + computed verdicts, the
census, ``expected_at_start`` re-admission, and target/self validation."""

from __future__ import annotations

import asyncio
import json
import logging

import fakeredis
import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

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

from .conftest import (
    _drop_presence,
    _FramePubsub,
    _make_nonmember_bus,
    _RaisingPubsub,
    _register_bare_presence,
    _spawn_subscriber,
    _stop,
    _until,
    make_bus,
)

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


async def test_publish_requires_a_non_empty_op_name() -> None:
    bus = make_bus()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    with pytest.raises(ValueError, match="non-empty 'op' name"):
        await bus.publish({}, targets=None, local=local)


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


# -- discard gates (generation / op_id) ---------------------------------------


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
            results = await bus._collect(client, _FramePubsub(frames), expected, {}, "op-A", "reload_config")
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
    bus = make_bus(ack_timeout=0.03, apply_timeout=0.2)
    bus._identity = WorkerIdentity(name="serve-9", kind=WorkerKind.serve, pid=1, generation=1)
    # A live presence key so the finalize presence re-check finds it alive → missing.
    await _register_bare_presence(server, bus._settings, "serve-1", WorkerKind.serve, ttl_ms=None)
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        results = await bus._collect(client, _RaisingPubsub(), {"serve-1": 1}, {}, "op-A", "reload_config")
        assert results["serve-1"].outcome == OpOutcome.missing
        assert "transport error" in (results["serve-1"].detail or "")
    finally:
        await client.aclose()


# -- missing vs departed (real TTL expiry) ------------------------------------


async def test_silent_but_present_worker_is_missing(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    publisher = make_bus()
    # Ready + fresh at the census (a comfortably-above-bound TTL that stays live through
    # the whole short apply window), so it is an EXPECTED worker — yet nobody replies.
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)

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
    # heartbeat_ttl=0.3 → freshness bound ≈ 100ms. A 200ms TTL is FRESH at the census (so
    # the target is expected) yet expires (200ms) well before the apply cut (600ms) — a
    # genuine TTL expiry mid-apply, reported departed.
    publisher = make_bus(heartbeat_ttl=0.3, ack_timeout=0.03, apply_timeout=0.6)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=200)

    result = await publisher.publish({"op": "reload_config"}, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.departed
    assert by_name["backend-1"].detail is not None


# -- whole-fleet gap outcomes (a row failing the ready+fresh gate is CLASSIFIED) ----


async def test_whole_fleet_publish_reports_a_resyncing_row(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A resyncing row is on the census but NOT ready, so it is not an expected worker; it
    # is landed as its ACTUAL condition (``resyncing``), never silently dropped and never
    # reported missing/timed_out. Its PTTL is fresh, so the written state drives it.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.resyncing
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.resyncing
    assert "resyncing" in (by_name["backend-1"].detail or "")


async def test_whole_fleet_publish_reports_a_recycling_row(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A fresh recycling row is landed as ``recycling`` — it is departing and converges by
    # old-life-gone + fresh capacity, not by a reply.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.recycling
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.recycling


async def test_whole_fleet_publish_reports_a_ready_but_decayed_row_as_stale(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A READY row whose remaining PTTL is below the freshness bound is a gap row: ready
    # alone is not enough, the presence must also be fresh. It is landed as ``stale`` (a
    # quiet row, reconnecting or dead), carrying no convergence promise. With
    # heartbeat_ttl=5.0 the bound is ttl/3 ≈ 1666ms, so a 1000ms PTTL fails it.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=1000, state=WorkerState.ready
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.stale


async def test_whole_fleet_publish_reports_a_decayed_resyncing_row_as_stale(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # A resyncing row whose PTTL has decayed maps to ``stale`` regardless of its written
    # state — a worker that died mid-resync must not carry a convergence promise.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=1000, state=WorkerState.resyncing
    )

    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.stale


async def test_targeted_gap_row_carries_its_gap_outcome(wire_bus_client: None, server: fakeredis.FakeServer) -> None:
    # A named target that is present-but-gap carries its gap outcome (not departed, not
    # awaited): a resyncing target is reported ``resyncing``.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.1)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.resyncing
    )

    result = await publisher.publish({"op": "reload_config"}, targets=["backend-1"], local=None)
    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.resyncing


async def test_targeted_worker_replaced_mid_apply_is_departed(
    wire_bus_client: None, server: fakeredis.FakeServer
) -> None:
    # The target is ready+fresh at publish (expected at generation 1) but nobody replies,
    # and by the report cut its presence key holds a HIGHER generation — a replacement took
    # the slot mid-apply. The generation-aware verdict is ``departed``, naming the
    # superseding generation, never timed_out/missing.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.2)
    settings = publisher._settings
    await _register_bare_presence(server, settings, "backend-1", WorkerKind.backend, ttl_ms=5000)

    async def bump_generation() -> None:
        # After the expected set is captured, rewrite the presence value at generation 2
        # (the freed slot reclaimed by a new life) with the key still live.
        await asyncio.sleep(0.06)
        client = aioredis.FakeRedis(server=server, decode_responses=True)
        now = "2026-01-01T00:00:00+00:00"
        value = json.dumps(
            {"kind": "backend", "pid": 9, "generation": 2, "joined_at": now, "beat_at": now, "state": "ready"}
        )
        await client.set(settings.presence_key("backend-1"), value, px=5000)
        await client.aclose()

    bumper = asyncio.create_task(bump_generation())
    try:
        result = await publisher.publish({"op": "reload_config"}, targets=["backend-1"], local=None)
    finally:
        await bumper
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
    publisher = make_bus(heartbeat_ttl=30.0, ack_timeout=0.05, apply_timeout=0.4)
    worker = make_bus(heartbeat_ttl=30.0, ack_timeout=0.05, apply_timeout=0.4)

    async def apply(_op: dict) -> None:
        return None

    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        await _drop_presence(server, publisher._settings, worker_id.name)
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local)
        assert [r.name for r in result.results] == [publisher.identity.name]
        assert result.ok
    finally:
        await _stop(task)


async def test_expected_at_start_still_collects_a_faded_workers_confirmation(wire_bus_client: None, server) -> None:
    # The fixed window: ready+fresh at op start, presence gone by publish, worker still
    # alive and replying. Carried in at its snapshot generation, so its terminal reply
    # passes the generation gate and lands as ``applied`` — the confirmation is collected,
    # not guessed at.
    publisher = make_bus(heartbeat_ttl=30.0, ack_timeout=0.05, apply_timeout=0.4)
    worker = make_bus(heartbeat_ttl=30.0, ack_timeout=0.05, apply_timeout=0.4)

    async def apply(_op: dict) -> dict:
        return {"reloaded": True}

    task, worker_id = await _spawn_subscriber(worker, apply)
    try:
        # The op-start census the publisher would have taken before its local apply.
        at_start = {row.name: row.generation for row in await publisher.census() if row.name == worker_id.name}
        assert at_start == {worker_id.name: worker_id.generation}

        await _drop_presence(server, publisher._settings, worker_id.name)
        local = LocalApplyResult(outcome=OpOutcome.applied)
        result = await publisher.publish({"op": "reload_config"}, targets=None, local=local, expected_at_start=at_start)

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
    # apply deadline bounds it — and the re-checked presence makes the verdict honest:
    # ``departed``, which drops the report off ``ok`` so the publisher logs it loudly.
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.2)
    await _register_bare_presence(server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000)
    at_start = {"backend-1": 1}
    await _drop_presence(server, publisher._settings, "backend-1")

    loop = asyncio.get_running_loop()
    start = loop.time()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish({"op": "reload_config"}, targets=None, local=local, expected_at_start=at_start)
    elapsed = loop.time() - start

    by_name = {r.name: r for r in result.results}
    assert by_name["backend-1"].outcome == OpOutcome.departed
    assert "presence key expired" in (by_name["backend-1"].detail or "")
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
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=5.0)
    await _register_bare_presence(
        server, publisher._settings, "backend-1", WorkerKind.backend, ttl_ms=5000, state=WorkerState.recycling
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    local = LocalApplyResult(outcome=OpOutcome.applied)
    result = await publisher.publish(
        {"op": "reload_config"}, targets=None, local=local, expected_at_start={"backend-1": 1}
    )
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
    publisher = make_bus(heartbeat_ttl=5.0, ack_timeout=0.03, apply_timeout=0.2)
    at_start = {"backend-2": 1, publisher.identity.name: 1}

    result = await publisher.publish(
        {"op": "reload_config"}, targets=["backend-1"], local=None, expected_at_start=at_start
    )

    assert [r.name for r in result.results] == ["backend-1"]


# -- a non-member (fork child) is never a fleet target ------------------------


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


# -- presence re-check degrade branches --------------------------------------


async def test_recheck_transport_error_degrades_to_loud_missing(caplog: pytest.LogCaptureFixture) -> None:
    # A transport error raised by the presence re-check (r.get) must not be swallowed
    # into a silent verdict: it logs an ERROR and degrades to the loud missing/timed_out
    # classification (a present-but-silent worker), never departed or dropped.
    bus = make_bus()

    class _BoomGet:
        async def get(self, key: str) -> str:
            raise RedisConnectionError("presence store down")

    with caplog.at_level(logging.ERROR):
        status, gen = await bus._recheck_presence(_BoomGet(), "serve-2")
        never_acked = await bus._computed_verdict(_BoomGet(), "serve-2", 1, acked=False, transport_error=None)
        acked = await bus._computed_verdict(_BoomGet(), "serve-2", 1, acked=True, transport_error=None)

    assert (status, gen) == ("unreachable", None)
    # Unreachable re-check is treated as still-present (not departed): missing when never
    # acked, timed_out when acked — the loud verdicts, not a silent drop.
    assert never_acked.outcome is OpOutcome.missing
    assert acked.outcome is OpOutcome.timed_out
    assert any(r.levelno == logging.ERROR and "presence re-check" in r.getMessage() for r in caplog.records), (
        "the transport-error degrade did not log loudly"
    )


async def test_recheck_unparseable_presence_logs_and_treats_alive(caplog: pytest.LogCaptureFixture) -> None:
    # An unparseable presence value at the report cut logs a WARNING and is treated as
    # ALIVE with an unknown generation — so the verdict is a present-but-silent one
    # (missing/timed_out), never departed (which would falsely claim the worker gone).
    bus = make_bus()

    class _Garbled:
        async def get(self, key: str) -> str:
            return "not-json"

    with caplog.at_level(logging.WARNING):
        status, gen = await bus._recheck_presence(_Garbled(), "serve-2")
        verdict = await bus._computed_verdict(_Garbled(), "serve-2", 1, acked=True, transport_error=None)

    assert (status, gen) == ("alive", None)
    assert verdict.outcome is OpOutcome.timed_out
    assert any(r.levelno == logging.WARNING and "unparseable" in r.getMessage() for r in caplog.records), (
        "the unparseable-value branch did not log a warning"
    )
