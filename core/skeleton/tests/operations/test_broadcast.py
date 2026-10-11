"""Oracles for the shared fleet-broadcast primitive's pre-apply ordering.

``broadcast`` reads everything the fleet report is judged against BEFORE it runs the
publisher's own local apply: the target validation (already pinned per-op) and, for an
untargeted op, the expected-confirmation membership. The membership snapshot is the
subject here — the local apply is arbitrarily slow, and :meth:`WorkerBus.publish`
censuses only at publish time, so a worker whose presence fades across the apply would
otherwise be off both the expected set and the gap set and the op would report converged
without it. :class:`FakeBus` records each publish's ``expected_at_start``, and its census
rows carry a settable PTTL, so an apply that fades a row is exactly what these drive.
"""

from __future__ import annotations

import logging
import signal

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerResult
from tai42_skeleton.operations import _broadcast
from tai42_skeleton.operations._broadcast import broadcast, fleet_fanout

from .._fakes.bus import FakeBus


def _install(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> FakeBus:
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


async def _applied() -> str:
    """A local apply that changes nothing the census can see."""
    return "applied"


async def test_untargeted_membership_is_censused_before_the_local_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window this pins: the sibling is ready+fresh when the op arrives, and its
    # presence decays below the freshness bound DURING the local apply. The snapshot is
    # read first, so the sibling is still owed a confirmation; a snapshot taken after the
    # apply (or no snapshot at all) would hand publish an empty membership and the op
    # would report converged without ever expecting it.
    bus = _install(monkeypatch, FakeBus(origin="serve-a", remotes=["serve-b"]))
    sibling = next(row for row in bus.rows if row.name == "serve-b")

    async def apply() -> str:
        sibling.pttl_ms = 0
        return "applied"

    await broadcast({"op": "reload_tool"}, None, apply)

    assert sibling.pttl_ms == 0
    assert bus.expected_at_start_calls == [{"serve-b": 1}]


async def test_untargeted_snapshot_excludes_self_and_gap_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    # The snapshot mirrors the bus's own expected-worker gate: the publisher reports
    # itself from its local result, and a row that already fails ready+fresh carries no
    # confirmation promise — carrying one in would turn a worker that is already known
    # to be departing or quiet into a wait.
    bus = _install(monkeypatch, FakeBus(origin="serve-a", remotes=["serve-b", "serve-c"]))
    next(row for row in bus.rows if row.name == "serve-c").pttl_ms = 0

    await broadcast({"op": "reload_tool"}, None, _applied)

    assert bus.expected_at_start_calls == [{"serve-b": 1}]


async def test_targeted_broadcast_takes_no_op_start_census(monkeypatch: pytest.MonkeyPatch) -> None:
    # A targeted op's expected set is the caller's explicit list, not the census, so a
    # named target that fades mid-apply is already carried into the report as departed.
    # The snapshot would be pure overhead — and a second census on a path that already
    # ran one for validation.
    bus = _install(monkeypatch, FakeBus(origin="serve-a", remotes=["serve-b"]))

    await broadcast({"op": "reload_tool"}, ["serve-b"], _applied)

    assert bus.validate_calls == [["serve-b"]]
    assert bus.expected_at_start_calls == [None]


async def test_op_start_census_failure_is_loud_but_never_aborts_the_op(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The snapshot is an enrichment on top of publish's own census; publish degrades a
    # dead bus to an honest unreachable report, so a census blip must not turn a
    # survivable op into a raise. It is logged, because the op then runs on exactly the
    # post-apply membership the snapshot exists to correct.
    bus = _install(monkeypatch, FakeBus(origin="serve-a", remotes=["serve-b"]))

    async def raising_census() -> dict[str, int]:
        raise ConnectionError("bus census unreachable")

    monkeypatch.setattr(bus, "expected_at_start", raising_census)

    with caplog.at_level(logging.WARNING, logger=_broadcast.logger.name):
        report = await broadcast({"op": "reload_tool"}, None, _applied)

    assert {row["name"]: row["outcome"] for row in report["results"]} == {"serve-a": "applied", "serve-b": "applied"}
    assert bus.expected_at_start_calls == [None]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == _broadcast.logger.name]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert "op-start census" in warnings[0].getMessage()


async def test_a_bus_broadcast_that_reached_no_sibling_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    # A broadcast that went over a configured bus but found no other worker on the
    # census (every sibling's presence gone by publish time) reads as exactly that,
    # never as a deployment with no bus.
    _install(monkeypatch, FakeBus(origin="serve-a", remotes=[]))
    over_the_bus = FleetResult(op="reload", results=[WorkerResult(name="serve-a", outcome=OpOutcome.applied)])
    assert fleet_fanout(over_the_bus) == {
        "mode": "local-only",
        "note": "no other worker is on the worker bus; only this worker reloaded",
    }


async def test_a_busless_broadcast_says_no_bus_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeBus(origin="serve-a", remotes=[]))
    busless = FleetResult(
        op="reload", local_only=True, results=[WorkerResult(name="serve-a", outcome=OpOutcome.applied)]
    )
    assert fleet_fanout(busless) == {
        "mode": "local-only",
        "note": "no worker bus configured; only this worker reloaded",
    }


async def test_fanout_body_carries_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every serialized fanout body carries convergence: the ``ok`` computed field
    # rides through the ``fleet`` and ``unreachable`` branches; the ``local-only``
    # branch reports a single worker applying and carries no fleet dump, so no ``ok``.
    _install(monkeypatch, FakeBus(origin="serve-a", remotes=["serve-b"]))

    partial = FleetResult(
        op="reload",
        results=[
            WorkerResult(name="serve-a", outcome=OpOutcome.applied),
            WorkerResult(name="serve-b", outcome=OpOutcome.failed, error="boom"),
        ],
    )
    fleet_body = fleet_fanout(partial)
    assert fleet_body["mode"] == "fleet"
    assert fleet_body["ok"] is False

    unreachable = FleetResult(op="reload", reachable=False, error="bus down")
    unreachable_body = fleet_fanout(unreachable)
    assert unreachable_body["mode"] == "unreachable"
    assert unreachable_body["ok"] is False

    local = FleetResult(op="reload", results=[WorkerResult(name="serve-a", outcome=OpOutcome.applied)])
    local_body = fleet_fanout(local)
    assert local_body["mode"] == "local-only"
    assert "ok" not in local_body


# -- the operation's verdict: workers still awaiting a respawn ------------------


def _row(name: str, keys: list[str] | None, outcome: str = "applied") -> WorkerResult:
    payload = None if keys is None else {"status": "ok", "env_keys": 2, "awaiting_respawn": keys}
    return WorkerResult(name=name, outcome=OpOutcome(outcome), payload=payload)


def _report(*rows: WorkerResult) -> FleetResult:
    return FleetResult(op="reload_config", results=list(rows))


_KEY = "BACKEND_MANIFEST_KEY"


def test_a_reload_that_rolls_nothing_leaves_every_listed_worker_awaiting() -> None:
    from tai42_skeleton.operations._broadcast import awaiting_respawn_after

    verdict = awaiting_respawn_after(
        _report(_row("serve-1", [_KEY]), _row("backend-1", []), _row("backend-2", None)),
        None,
        None,
        self_exit_armed=False,
    )

    assert [(w.name, w.keys) for w in verdict] == [("serve-1", [_KEY])]


def test_a_converged_roll_with_the_writer_armed_leaves_nobody_awaiting() -> None:
    from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
    from tai42_skeleton.operations._broadcast import awaiting_respawn_after

    roll = RecycleReport(rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)])
    writer = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)

    verdict = awaiting_respawn_after(
        _report(_row("serve-1", [_KEY]), _row("backend-1", [_KEY])), roll, writer, self_exit_armed=True
    )

    assert verdict == []


def test_a_stopped_roll_leaves_its_unreached_and_stopped_targets_and_the_writer_awaiting() -> None:
    from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleReport, RecycleRow, RecycleStop
    from tai42_skeleton.operations._broadcast import awaiting_respawn_after

    roll = RecycleReport(
        rows=[
            RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED),
            RecycleRow(name="backend-2", kind="backend", generation_before=1, status=TIMED_OUT, detail="slow"),
        ],
        stopped=RecycleStop(kind="backend", name="backend-2", detail="slow"),
    )
    writer = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    report = _report(
        _row("serve-1", [_KEY]), _row("backend-1", [_KEY]), _row("backend-2", [_KEY]), _row("serve-2", [_KEY])
    )

    verdict = awaiting_respawn_after(report, roll, writer, self_exit_armed=False)

    assert [w.name for w in verdict] == ["backend-2", "serve-1", "serve-2"]


def test_the_awaiting_verdict_is_logged_once_naming_every_worker(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.operations._broadcast import log_awaiting_respawn

    with caplog.at_level(logging.ERROR):
        log_awaiting_respawn([])
        log_awaiting_respawn(
            [AwaitingWorker(name="backend-2", keys=[_KEY]), AwaitingWorker(name="serve-1", keys=[_KEY])]
        )

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [
        "worker(s) awaiting a respawn — recycle-class key(s) changed in the stored env since they were bound and "
        "nothing in this operation respawns them: backend-2 (BACKEND_MANIFEST_KEY), serve-1 (BACKEND_MANIFEST_KEY) — "
        "restart each through the deployment's supervisor (bare: restart the server)"
    ]


def _fork_child_bus(monkeypatch: pytest.MonkeyPatch) -> FakeBus:
    """A forked job child's bus: a non-member acting for its member parent ``backend-1``."""
    from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind

    bus = FakeBus(origin="backend-1/fork-7", remotes=["backend-1"])
    bus._identity = WorkerIdentity(name="backend-1/fork-7", kind=WorkerKind.backend, pid=7, generation=0, member=False)
    bus._member = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=1, generation=3)
    return _install(monkeypatch, bus)


async def test_a_non_member_publisher_applies_nothing_locally_and_its_member_converges_as_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fork child is never a fleet target: it has no serving surface of its own, so the
    # whole-fleet op runs no local apply there and publishes with no self entry; the member
    # parent applies it as a sibling.
    bus = _fork_child_bus(monkeypatch)
    applied: list[str] = []

    async def apply() -> str:
        applied.append("local")
        return "applied"

    body = await broadcast({"op": "reload_tool"}, None, apply)

    assert applied == []
    assert [call[2] for call in bus.publish_calls] == [None]
    assert [row["name"] for row in body["results"]] == ["backend-1"]


async def test_the_writer_exit_of_a_non_member_aims_at_the_member_it_acts_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app import graceful_exit
    from tai42_skeleton.operations._broadcast import writer_exit_response

    _fork_child_bus(monkeypatch)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(graceful_exit.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    response = writer_exit_response({"status": "ok"}, armed=True)
    assert response.background is not None
    await response.background()

    assert kills == [(1, signal.SIGTERM)]


async def test_the_writer_exit_of_a_member_aims_at_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app import graceful_exit
    from tai42_skeleton.operations._broadcast import writer_exit_response

    bus = _install(monkeypatch, FakeBus(origin="serve-a"))
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(graceful_exit.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    response = writer_exit_response({"status": "ok"}, armed=True)
    assert response.background is not None
    await response.background()

    assert kills == [(bus.identity.pid, signal.SIGTERM)]


async def test_a_preset_fan_out_from_a_non_member_carries_no_self_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # A preset write run inside a forked job child: the child is never a fleet target, so its
    # reload and removal fan-outs publish no self entry and the member parent loads the change as
    # a sibling.
    from tai42_skeleton.operations.presets import fanout

    bus = _fork_child_bus(monkeypatch)

    reloaded = await fanout._fanout_reload("p1", None)
    removed = await fanout._fanout_remove("p1", None)

    assert [call[2] for call in bus.publish_calls] == [None, None]
    assert [row.name for row in reloaded.results] == ["backend-1"]
    assert [row.name for row in removed.results] == ["backend-1"]


async def test_a_preset_fan_out_from_a_member_carries_its_applied_self_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.operations.presets import fanout

    bus = _install(monkeypatch, FakeBus(origin="serve-a", remotes=["serve-b"]))

    await fanout._fanout_reload("p1", None)

    local = bus.publish_calls[0][2]
    assert local is not None
    assert local.outcome == OpOutcome.applied
