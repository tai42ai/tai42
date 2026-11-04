"""The roll acts on the life that is ready, never on the reload report's earlier sample alone.

Driven through the one decide-and-roll path with the real recycle orchestrator over a scripted
fleet: a worker whose reload stated recycle-class keys is restarted at once when the ready life is
the one that stated them; every other worker the roll visits is first asked with a targeted
``reload_config`` and its reply decides — clean is left running, keys restart it with those keys,
no statement restarts it as unknown. A worker whose reload was refused at its epoch build is never
restarted, even when it states keys: its restart is withheld and the verdict names it. A reload
naming its targets visits no other worker.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from tai42_skeleton.app.bus import (
    FleetResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
)
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.discarded_build import DISCARDED_BUILD_STATUS
from tai42_skeleton.app.recycle import FAILED, RECYCLED, orchestrate_recycle
from tai42_skeleton.config import recycle_policy, recycle_roll
from tai42_skeleton.config.recycle_roll import RollOutcome, decide_and_roll
from tai42_skeleton.operations._broadcast import roll_outcome_body

_NOW = "2026-01-01T00:00:00+00:00"
_KEY = "TAI_BUS_HEARTBEAT_TTL"
_WRITER = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=5)


def _census_row(name: str, *, generation: int = 1, state: WorkerState = WorkerState.ready) -> WorkerRow:
    return WorkerRow(
        name=name,
        kind=WorkerKind.serve,
        pid=1,
        generation=generation,
        joined_at=_NOW,
        beat_at=_NOW,
        state=state,
        deployment_keys=[],
        env_readers="",
        pttl_ms=15_000,
    )


def _reply(name: str, outcome: OpOutcome = OpOutcome.applied, keys: list[str] | None = None) -> WorkerResult:
    payload = None if outcome is not OpOutcome.applied else {"status": "ok", "awaiting_respawn": keys or []}
    return WorkerResult(name=name, outcome=outcome, payload=payload)


class _Fleet:
    """A scripted fleet: a ``reload_config`` ask answers from ``answers`` (in order, per worker); a recycle respawns.

    ``after_ask`` maps a worker to the census rows that replace its row once it answered an ask
    (a life that ends during the ask).
    """

    heartbeat_ttl = 15.0

    def __init__(
        self,
        rows: list[WorkerRow],
        answers: dict[str, list[WorkerResult]] | None = None,
        *,
        after_ask: dict[str, WorkerRow] | None = None,
        reachable: bool = True,
    ) -> None:
        self.rows = list(rows)
        self.answers = {name: list(replies) for name, replies in (answers or {}).items()}
        self.after_ask = dict(after_ask or {})
        self.reachable = reachable
        self.published: list[tuple[str, str]] = []

    async def census(self) -> list[WorkerRow]:
        return list(self.rows)

    async def publish(self, op: FleetOp, targets: list[str] | None, local: Any) -> FleetResult:
        assert targets is not None
        [name] = targets
        self.published.append((op.op, name))
        if not self.reachable:
            return FleetResult(op=op.op, reachable=False, error="ConnectionError: bus down")
        if op.op == "reload_config":
            reply = self.answers[name].pop(0)
            if name in self.after_ask:
                self.rows = [row for row in self.rows if row.name != name] + [self.after_ask.pop(name)]
            return FleetResult(op=op.op, results=[reply])
        old = next(row for row in self.rows if row.name == name)
        self.rows = [row for row in self.rows if row.name != name] + [_census_row(name, generation=old.generation + 1)]
        return FleetResult(op=op.op, results=[WorkerResult(name=name, outcome=OpOutcome.applied)])


@pytest.fixture(autouse=True)
def _short_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recycle_roll, "recycle_step_timeout", lambda: 1.0)


async def _roll(
    fleet: _Fleet,
    results: list[WorkerResult],
    pinned: dict[str, int],
    *,
    reachable: bool = True,
    addressed: set[str] | None = None,
    writer_reply: WorkerResult | None = None,
) -> RollOutcome:
    writer = writer_reply or _reply(_WRITER.name)
    report = FleetResult(op="reload_config", reachable=reachable, results=[writer, *results])
    return await decide_and_roll(
        cast("WorkerBus", fleet),
        report,
        _WRITER,
        {name: (WorkerKind.serve, generation) for name, generation in pinned.items()},
        change="this config change",
        orchestrate=orchestrate_recycle,
        addressed=addressed,
    )


_WITHHELD_ADVICE = (
    "worker(s) whose reload was refused at its epoch build — the stored config did not build on them, so each keeps "
    "serving its current config, its recycle-class key(s) changed in the stored env since they were bound: "
    "{workers} — fix the stored config, or the cause the build error names, before restarting any of them"
)


def _lines(outcome: RollOutcome) -> list[tuple[str, str, list[str], str | None]]:
    body = roll_outcome_body(outcome)
    assert body["recycle"] is not None
    return [(line["name"], line["status"], line["keys"], line["detail"]) for line in body["recycle"]["recycle"]]


async def test_a_sibling_busy_with_its_own_reload_is_asked_once_ready_and_left_running_when_clean() -> None:
    # Two concurrent hot writes: serve-2 missed this write's apply deadline inside its own reload.
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")], {"serve-2": [_reply("serve-2")]})

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.timed_out)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.rows == []
    assert outcome.recycle.confirmed_clean == ["serve-2"]
    body = roll_outcome_body(outcome)
    assert body["recycle"]["skipped"] == ["serve-2"]
    assert body["awaiting_respawn"] == []


async def test_a_resyncing_sibling_that_confirms_keys_once_ready_is_restarted_with_them() -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2")], {"serve-2": [_reply("serve-2", keys=[_KEY])]}
    )

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.resyncing)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2"), ("recycle", "serve-2")]
    assert _lines(outcome) == [
        ("serve-2", RECYCLED, [_KEY], "its reload reported resyncing; confirmed once ready"),
    ]


async def test_a_sibling_still_silent_when_asked_once_ready_is_restarted_as_unknown_naming_both_asks() -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2")],
        {"serve-2": [_reply("serve-2", OpOutcome.missing)]},
    )

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.timed_out)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2"), ("recycle", "serve-2")]
    assert _lines(outcome) == [
        (
            "serve-2",
            RECYCLED,
            [],
            "its reload reported timed_out; asked once ready: its reload reported missing",
        ),
    ]


def _refused_at_its_build(name: str, keys: list[str] | None = None) -> WorkerResult:
    """The reply of a worker whose reload was refused at its epoch build: failed, stating its drift after discard."""
    return WorkerResult(
        name=name,
        outcome=OpOutcome.failed,
        payload={"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": keys or []},
        error="SchemaOutOfDateError: plugin migration chain pending",
    )


async def test_a_sibling_whose_reload_was_refused_at_its_epoch_build_is_neither_asked_nor_restarted() -> None:
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")])

    outcome = await _roll(fleet, [_refused_at_its_build("serve-2")], {"serve-2": 1})

    assert fleet.published == []
    assert outcome.recycle is None or outcome.recycle.rows == []


async def test_a_sibling_refused_at_its_build_still_owing_a_respawn_is_left_running_and_named_in_the_verdict() -> None:
    # serve-2 still runs on the value of a key an earlier change moved; its build of the stored
    # config failed, and a restart may fail the same way.
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2"), _census_row("serve-3")])

    outcome = await _roll(
        fleet,
        [_refused_at_its_build("serve-2", [_KEY]), _reply("serve-3", keys=[_KEY])],
        {"serve-2": 1, "serve-3": 1},
    )

    assert fleet.published == [("recycle", "serve-3")]
    body = roll_outcome_body(outcome)
    assert body["awaiting_respawn"] == [{"name": "serve-2", "keys": [_KEY]}]
    assert body["recycle"]["skipped"] == []


async def test_a_skipped_roll_names_the_sibling_whose_restart_it_withholds_and_tells_to_fix_the_config_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")])

    with caplog.at_level("INFO"):
        outcome = await _roll(fleet, [_refused_at_its_build("serve-2", [_KEY])], {"serve-2": 1})

    assert fleet.published == []
    assert outcome.recycle is None
    assert (
        "the fleet recycle for this config change is skipped: no worker needs a restart (confirmed clean: none; "
        f"left running, its restart withheld: serve-2 (reports {_KEY}); the writer 'serve-1' is clean)"
    ) in caplog.text
    assert _WITHHELD_ADVICE.format(workers=f"serve-2 ({_KEY})") in caplog.text
    assert "restart each through the deployment's supervisor" not in caplog.text


async def test_a_writer_whose_reload_failed_at_its_build_is_told_to_fix_the_stored_config_before_any_restart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")])

    with caplog.at_level("ERROR"):
        outcome = await _roll(
            fleet,
            [_reply("serve-2")],
            {"serve-2": 1},
            writer_reply=_refused_at_its_build(_WRITER.name, [_KEY]),
        )

    assert fleet.published == []
    assert not outcome.self_exit_armed
    assert roll_outcome_body(outcome)["awaiting_respawn"] == [{"name": "serve-1", "keys": [_KEY]}]
    assert _WITHHELD_ADVICE.format(workers=f"serve-1 ({_KEY})") in caplog.text
    assert "restart each through the deployment's supervisor" not in caplog.text


async def test_a_sibling_whose_reload_is_refused_at_its_epoch_build_when_asked_once_ready_is_left_running() -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2")],
        {"serve-2": [_refused_at_its_build("serve-2")]},
    )

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.timed_out)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.rows == []
    assert outcome.recycle.confirmed_clean == ["serve-2"]


async def test_a_sibling_refused_at_its_build_when_asked_once_ready_still_owing_a_respawn_is_withheld(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2")],
        {"serve-2": [_refused_at_its_build("serve-2", [_KEY])]},
    )

    with caplog.at_level("ERROR"):
        outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.timed_out)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.confirmed_clean == []
    assert outcome.recycle.converged
    assert _lines(outcome) == [
        (
            "serve-2",
            "withheld",
            [_KEY],
            f"its reload reported timed_out; asked once ready: {recycle_policy.REFUSED_BUILD}",
        ),
    ]
    assert roll_outcome_body(outcome)["awaiting_respawn"] == [{"name": "serve-2", "keys": [_KEY]}]
    assert _WITHHELD_ADVICE.format(workers=f"serve-2 ({_KEY})") in caplog.text
    assert "restart each through the deployment's supervisor" not in caplog.text


async def test_a_sibling_whose_reload_fails_any_other_way_when_asked_once_ready_is_restarted_as_unknown() -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2")],
        {"serve-2": [_reply("serve-2", OpOutcome.failed)]},
    )

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.failed)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2"), ("recycle", "serve-2")]
    assert _lines(outcome) == [
        ("serve-2", RECYCLED, [], "its reload reported failed; asked once ready: its reload reported failed"),
    ]


async def test_the_life_that_reported_keys_is_restarted_without_a_second_ask() -> None:
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")])

    outcome = await _roll(fleet, [_reply("serve-2", keys=[_KEY])], {"serve-2": 1})

    assert fleet.published == [("recycle", "serve-2")]
    assert _lines(outcome) == [("serve-2", RECYCLED, [_KEY], None)]


async def test_a_new_life_in_place_of_the_one_that_reported_keys_is_asked_and_left_running_when_clean() -> None:
    # Another roll already replaced serve-2: the life now ready booted on the stored env.
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2", generation=2)],
        {"serve-2": [_reply("serve-2")]},
    )

    outcome = await _roll(fleet, [_reply("serve-2", keys=[_KEY])], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.confirmed_clean == ["serve-2"]
    # The old life's keys are not the new life's: nobody is left awaiting a respawn.
    assert outcome.awaiting_respawn == []


async def test_the_successor_of_a_sibling_mid_recycle_is_asked_not_recycled_again() -> None:
    # serve-2 was mid-recycle when the report was collected; its successor is the life now ready.
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2", generation=2)],
        {"serve-2": [_reply("serve-2")]},
    )

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.recycling)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.rows == []
    assert outcome.recycle.converged


async def test_a_life_that_ends_during_the_ask_has_its_next_life_asked() -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2")],
        {"serve-2": [_reply("serve-2", OpOutcome.departed), _reply("serve-2")]},
        after_ask={"serve-2": _census_row("serve-2", generation=2)},
    )

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.timed_out)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2"), ("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.confirmed_clean == ["serve-2"]


async def test_an_ask_the_bus_cannot_carry_stops_the_roll_at_that_worker() -> None:
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")], reachable=False)

    outcome = await _roll(fleet, [_reply("serve-2", OpOutcome.timed_out)], {"serve-2": 1})

    assert fleet.published == [("reload_config", "serve-2")]
    assert outcome.recycle is not None
    assert outcome.recycle.stopped is not None
    assert outcome.recycle.stopped.name == "serve-2"
    assert [(row.name, row.status) for row in outcome.recycle.rows] == [("serve-2", FAILED)]
    assert not outcome.self_exit_armed


async def test_a_worker_that_joined_after_the_change_began_is_asked_on_a_whole_fleet_roll() -> None:
    fleet = _Fleet(
        [_census_row("serve-1", generation=5), _census_row("serve-2"), _census_row("serve-3")],
        {"serve-3": [_reply("serve-3")]},
    )

    outcome = await _roll(fleet, [_reply("serve-2", keys=[_KEY])], {"serve-2": 1})

    assert fleet.published == [("recycle", "serve-2"), ("reload_config", "serve-3")]
    assert outcome.recycle is not None
    assert outcome.recycle.confirmed_clean == ["serve-3"]


async def test_a_reload_naming_its_targets_never_visits_a_worker_it_did_not_address() -> None:
    # serve-3 joined after the census the change pinned; the reload named serve-2 alone.
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2"), _census_row("serve-3")])

    outcome = await _roll(fleet, [_reply("serve-2", keys=[_KEY])], {"serve-2": 1}, addressed={"serve-2"})

    assert fleet.published == [("recycle", "serve-2")]
    assert [line[0] for line in _lines(outcome)] == ["serve-2"]


async def test_an_unreachable_report_still_counts_the_row_it_carries() -> None:
    # A joiner's failed announcement carries the joiner's own known reload row: the joiner is
    # judged clean from it, and the siblings, unknown, are asked once ready.
    fleet = _Fleet([_census_row("serve-1", generation=5), _census_row("serve-2")], {"serve-2": [_reply("serve-2")]})

    outcome = await _roll(fleet, [], {"serve-2": 1}, reachable=False)

    assert outcome.decision.writer_reason is None
    assert fleet.published == [("reload_config", "serve-2")]
    assert not outcome.self_exit_armed
