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

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.operations import _broadcast
from tai42_skeleton.operations._broadcast import broadcast
from tests._fakes.bus import FakeBus


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
