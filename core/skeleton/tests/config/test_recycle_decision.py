"""Which workers a fleet recycle restarts, asks once ready, or leaves running, decided from one reload report."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerBus, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.app.discarded_build import DISCARDED_BUILD_STATUS
from tai42_skeleton.app.recycle import RecycleReport
from tai42_skeleton.config import recycle_policy
from tai42_skeleton.config.recycle_policy import (
    MALFORMED_REPLY,
    NO_REPORT_ROW,
    UNREACHABLE_REPORT,
    RecycleReason,
    ReportedKeys,
    recycle_decision,
)
from tai42_skeleton.config.recycle_roll import decide_and_roll

from .._fakes.bus import FakeBus

WRITER = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=3)
PINNED = {"serve-2": (WorkerKind.serve, 1), "backend-1": (WorkerKind.backend, 4)}


def _row(name: str, outcome: OpOutcome = OpOutcome.applied, payload: Any = None) -> WorkerResult:
    return WorkerResult(name=name, outcome=outcome, payload=payload)


def _clean(name: str) -> WorkerResult:
    return _row(name, payload={"status": "ok", "env_keys": 2, "awaiting_respawn": []})


def _report(*rows: WorkerResult) -> FleetResult:
    return FleetResult(op="reload_config", results=list(rows))


def test_every_worker_that_confirms_a_clean_reload_is_left_running() -> None:
    decision = recycle_decision(_report(_clean("serve-1"), _clean("serve-2"), _clean("backend-1")), PINNED, WRITER)

    assert decision.reported == {}
    assert decision.unconfirmed == {}
    assert decision.clean == {"serve-2", "backend-1"}
    assert decision.writer_reason is None
    assert not any(decision.rolls_over(name) for name in PINNED)


def test_a_pinned_worker_reporting_recycle_class_keys_is_restarted_as_the_life_that_reported_them() -> None:
    report = _report(
        _clean("serve-1"), _clean("serve-2"), _row("backend-1", payload={"awaiting_respawn": ["TAI_POOL", "TAI_X"]})
    )

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.reported == {"backend-1": ReportedKeys(generation=4, keys=("TAI_POOL", "TAI_X"))}
    assert decision.clean == {"serve-2"}
    assert decision.rolls_over("backend-1")
    assert not decision.writer_needs


@pytest.mark.parametrize("outcome", [o for o in OpOutcome if o is not OpOutcome.applied])
def test_a_worker_whose_reload_did_not_apply_has_an_unknown_state_and_is_confirmed_at_the_roll(
    outcome: OpOutcome,
) -> None:
    report = _report(_clean("serve-1"), _row("serve-2", outcome), _clean("backend-1"))

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.unconfirmed == {"serve-2": f"its reload reported {outcome.value}"}
    assert decision.reported == {}
    assert decision.rolls_over("serve-2")


def test_a_pinned_worker_with_no_report_row_is_confirmed_at_the_roll() -> None:
    decision = recycle_decision(_report(_clean("serve-1"), _clean("serve-2")), PINNED, WRITER)

    assert decision.unconfirmed == {"backend-1": NO_REPORT_ROW}


def test_an_unreachable_report_leaves_every_pinned_worker_and_the_writer_unknown() -> None:
    decision = recycle_decision(FleetResult(op="reload_config", reachable=False, error="down"), PINNED, WRITER)

    assert decision.unconfirmed == dict.fromkeys(PINNED, UNREACHABLE_REPORT)
    assert decision.writer_reason == RecycleReason(unknown=UNREACHABLE_REPORT)


def test_an_unreachable_report_still_judges_the_rows_it_carries() -> None:
    report = FleetResult(op="reload_config", reachable=False, error="down", results=[_clean("serve-1")])

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.writer_reason is None
    assert decision.unconfirmed == dict.fromkeys(PINNED, UNREACHABLE_REPORT)


@pytest.mark.parametrize("payload", [None, {}, {"awaiting_respawn": "TAI_X"}, {"awaiting_respawn": [1]}])
def test_an_applied_reply_without_a_well_formed_list_is_confirmed_at_the_roll_and_logged_at_error(
    payload: Any, caplog: pytest.LogCaptureFixture
) -> None:
    report = _report(_clean("serve-1"), _row("serve-2", payload=payload), _clean("backend-1"))

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.config.recycle_policy"):
        decision = recycle_decision(report, PINNED, WRITER)

    assert decision.unconfirmed == {"serve-2": MALFORMED_REPLY}
    assert any("'serve-2'" in record.getMessage() for record in caplog.records if record.levelno == logging.ERROR)


def test_the_writer_reporting_keys_needs_its_own_restart() -> None:
    report = _report(_row("serve-1", payload={"awaiting_respawn": ["TAI_X"]}), _clean("serve-2"), _clean("backend-1"))

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.writer_reason == RecycleReason(keys=("TAI_X",))
    assert decision.writer_needs
    assert decision.reported == {}
    assert not decision.rolls_over("serve-1")


def test_a_writer_with_no_row_needs_its_own_restart() -> None:
    decision = recycle_decision(_report(_clean("serve-2"), _clean("backend-1")), PINNED, WRITER)

    assert decision.writer_reason == RecycleReason(unknown=NO_REPORT_ROW)


def _refused(name: str, payload: Any) -> WorkerResult:
    """The row of a worker whose reload was refused at its epoch build, its payload ``payload``."""
    return _row(name, OpOutcome.failed, payload=payload)


def test_a_writer_whose_discarded_build_measures_no_drift_needs_no_restart() -> None:
    report = _report(
        _refused("serve-1", {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": []}),
        _clean("serve-2"),
        _clean("backend-1"),
    )

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.writer_reason is None
    assert not decision.writer_needs


def test_a_pinned_sibling_whose_discarded_build_reports_keys_is_withheld_never_rolled_over() -> None:
    # The stored config did not build on serve-2 and a restart may fail the same way, so the
    # keys it still owes a respawn for are stated, not acted on.
    report = _report(
        _clean("serve-1"),
        _refused("serve-2", {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": ["TAI_X"]}),
        _clean("backend-1"),
    )

    decision = recycle_decision(report, PINNED, WRITER)

    assert "serve-2" not in decision.clean
    assert decision.reported == {}
    assert decision.unconfirmed == {}
    assert decision.withheld == {"serve-2": ("TAI_X",)}
    assert not decision.rolls_over("serve-2")


def test_an_unpinned_sibling_whose_discarded_build_reports_keys_is_withheld_too() -> None:
    report = _report(
        _clean("serve-1"), _refused("serve-9", {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": ["TAI_X"]})
    )

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.withheld == {"serve-9": ("TAI_X",)}
    assert not decision.rolls_over("serve-9")


def test_a_writer_whose_discarded_build_reports_keys_owes_a_restart_its_roll_withholds() -> None:
    report = _report(
        _refused("serve-1", {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": ["TAI_X"]}),
        _clean("serve-2"),
        _clean("backend-1"),
    )

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.writer_reason == RecycleReason(keys=("TAI_X",), withheld=recycle_policy.REFUSED_BUILD)
    assert not decision.writer_needs


@pytest.mark.parametrize(
    "payload", [{"status": DISCARDED_BUILD_STATUS}, {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": [1]}]
)
def test_a_discarded_build_statement_without_a_well_formed_list_leaves_the_state_unknown(
    payload: Any, caplog: pytest.LogCaptureFixture
) -> None:
    report = _report(_clean("serve-1"), _refused("serve-2", payload), _clean("backend-1"))

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.config.recycle_policy"):
        decision = recycle_decision(report, PINNED, WRITER)

    assert decision.unconfirmed == {"serve-2": MALFORMED_REPLY}
    assert any("'serve-2'" in record.getMessage() for record in caplog.records if record.levelno == logging.ERROR)


def test_a_writer_whose_reload_failed_without_stating_a_discarded_build_needs_its_own_restart() -> None:
    report = _report(_row("serve-1", OpOutcome.failed), _clean("serve-2"), _clean("backend-1"))

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.writer_reason == RecycleReason(unknown="its reload reported failed")
    assert decision.writer_needs


@pytest.mark.parametrize("outcome", [o for o in OpOutcome if o not in (OpOutcome.applied, OpOutcome.failed)])
def test_a_discarded_build_statement_on_a_row_that_is_not_failed_leaves_the_state_unknown(outcome: OpOutcome) -> None:
    report = _report(_row("serve-1", outcome, payload={"status": DISCARDED_BUILD_STATUS}), _clean("serve-2"))

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.writer_reason == RecycleReason(unknown=f"its reload reported {outcome.value}")


def test_a_row_for_an_unpinned_worker_is_judged_too_and_confirmed_at_the_roll() -> None:
    report = _report(_clean("serve-1"), _clean("serve-2"), _clean("backend-1"), _row("serve-9", OpOutcome.timed_out))

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.unconfirmed == {"serve-9": "its reload reported timed_out"}


def test_keys_from_a_worker_the_change_did_not_pin_are_confirmed_at_the_roll() -> None:
    report = _report(
        _clean("serve-1"),
        _clean("serve-2"),
        _clean("backend-1"),
        _row("serve-9", payload={"awaiting_respawn": ["TAI_X"]}),
    )

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.reported == {}
    assert decision.unconfirmed == {"serve-9": "its reload reported TAI_X from a life the change did not pin"}


def test_a_targeted_reload_judges_and_rolls_over_only_the_addressed_workers() -> None:
    report = _report(_row("serve-2", payload={"awaiting_respawn": ["TAI_X"]}))

    decision = recycle_decision(report, PINNED, WRITER, addressed={"serve-2"})

    assert decision.reported == {"serve-2": ReportedKeys(generation=1, keys=("TAI_X",))}
    assert decision.unconfirmed == {}
    assert decision.writer_reason is None
    assert decision.rolls_over("serve-2")
    # A worker that joined after the change began, or one the reload did not name, is never visited.
    assert not decision.rolls_over("backend-1")
    assert not decision.rolls_over("serve-7")


def test_a_targeted_reload_that_addresses_the_writer_judges_it() -> None:
    decision = recycle_decision(_report(_clean("serve-2")), PINNED, WRITER, addressed={"serve-1", "serve-2"})

    assert decision.writer_reason == RecycleReason(unknown=NO_REPORT_ROW)
    assert not decision.rolls_over("backend-1")


def test_a_worker_the_decision_never_saw_is_asked_for_having_no_report_row() -> None:
    decision = recycle_decision(_report(_clean("serve-1")), {}, WRITER)

    assert decision.rolls_over("serve-7")
    assert decision.why_unconfirmed("serve-7") == NO_REPORT_ROW


def test_an_unreachable_report_leaves_every_worker_the_roll_finds_to_be_asked_for_the_unreached_bus() -> None:
    report = FleetResult(op="reload_config", reachable=False, error="down", results=[_clean("serve-1")])

    decision = recycle_decision(report, {}, WRITER)

    assert decision.report_unreachable
    assert decision.rolls_over("serve-7")
    assert decision.why_unconfirmed("serve-7") == UNREACHABLE_REPORT


def test_a_reachable_report_knows_the_fleet_it_judged() -> None:
    decision = recycle_decision(_report(_clean("serve-1"), _clean("serve-2")), {}, WRITER)

    assert not decision.report_unreachable


def test_a_worker_whose_reporting_life_is_gone_is_asked_naming_that_life() -> None:
    report = _report(_clean("serve-1"), _clean("serve-2"), _row("backend-1", payload={"awaiting_respawn": ["TAI_X"]}))

    decision = recycle_decision(report, PINNED, WRITER)

    assert decision.why_unconfirmed("backend-1") == (
        "its reload reported TAI_X as generation 4, a life no longer on the census"
    )


@pytest.mark.parametrize(
    ("addressed", "asked"),
    [
        (None, "every other worker the roll's census shows"),
        ({"backend-1", "serve-2"}, "every other worker the reload named (backend-1, serve-2) the roll's census shows"),
    ],
    ids=["whole-fleet", "targeted"],
)
async def test_the_decision_line_names_whom_an_unreachable_report_leaves_the_roll_to_ask(
    addressed: set[str] | None, asked: str, caplog: pytest.LogCaptureFixture
) -> None:
    # A targeted reload's roll visits only the workers the reload named, so the line says so.
    bus = FakeBus(origin="serve-1", remotes=["serve-2", "backend-1", "serve-7"])

    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        return RecycleReport()

    report = FleetResult(op="reload_config", reachable=False, error="down")
    with caplog.at_level(logging.INFO):
        await decide_and_roll(
            cast("WorkerBus", bus),
            report,
            bus.member_identity,
            {},
            change="this config change",
            orchestrate=orchestrate,
            writer_exits=False,
            addressed=addressed,
        )

    assert f"confirm once ready {asked} ({UNREACHABLE_REPORT});" in caplog.text
