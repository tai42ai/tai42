"""Applying a pending save on real Postgres: one transaction, per-key FIFO, held rows, failures, the loud path."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from psycopg.errors import AdminShutdown
from tai42_contract.errors import ErrorKind
from tai42_contract.states.errors import StatePendingSaveFailedError, ValueValidationError

from tai42_skeleton.states.outbox import apply as apply_mod
from tai42_skeleton.states.outbox import loud as loud_mod
from tai42_skeleton.states.outbox.apply import apply_row, in_flight_tasks, is_transient
from tai42_skeleton.states.outbox.metrics import outbox_metrics

from .conftest import OutboxBed, execute, set_states_env

pytestmark = pytest.mark.integration


def _set(field: str, value: Any) -> dict[str, Any]:
    return {"op": "set", "path": [field], "value": value}


async def test_the_records_and_the_rows_removal_commit_together(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await bed.enqueue(bed.write(bed.subject(), [_set("n", 1)]))
    real_finish = bed.svc._store.outbox_finish_records

    async def _kill_then_finish(conn: Any, row_id: int, *, has_calls: bool) -> None:
        # The connection dies between the record write and the commit.
        await conn.execute("SELECT pg_terminate_backend(pg_backend_pid())")
        await real_finish(conn, row_id, has_calls=has_calls)

    monkeypatch.setattr(bed.svc._store, "outbox_finish_records", _kill_then_finish)
    outcome = await apply_row(bed.svc, row)
    assert outcome.outcome == "retry"  # a lost connection is transient
    assert outcome.error is not None
    assert "terminating connection due to administrator command" in str(outcome.error)
    assert await bed.status(row) == "pending"
    assert await execute("SELECT data FROM state_records WHERE state = %s", (bed.state,)) == []
    monkeypatch.setattr(bed.svc._store, "outbox_finish_records", real_finish)
    await execute("UPDATE state_outbox SET next_attempt_at = NULL WHERE id = %s", (row,))
    assert (await apply_row(bed.svc, row)).outcome == "applied"
    assert await bed.status(row) is None
    assert await execute("SELECT data FROM state_records WHERE state = %s", (bed.state,)) == [({"n": 1},)]


async def test_two_rows_on_one_subject_apply_in_id_order_whatever_the_arrival(bed: OutboxBed) -> None:
    first, second = await bed.enqueue_together(
        [bed.write(bed.subject(), [_set("n", 1)])], [bed.write(bed.subject(), [_set("n", 2)])]
    )
    # The newer row is driven first and concurrently with the older one.
    outcomes = await asyncio.gather(apply_row(bed.svc, second), apply_row(bed.svc, first))
    assert {o.outcome for o in outcomes} <= {"applied", "not_pending"}
    rows = await execute("SELECT paths, run_id FROM state_writes WHERE state = %s ORDER BY id", (bed.state,))
    assert len(rows) == 2
    assert await execute("SELECT data FROM state_records WHERE state = %s", (bed.state,)) == [({"n": 2},)]
    assert await bed.status(first) is None
    assert await bed.status(second) is None


async def test_an_older_failed_row_holds_a_newer_one_sharing_a_record_key(bed: OutboxBed) -> None:
    held_by, newer = await bed.enqueue_together(
        [bed.write(bed.subject("A"), [_set("n", 1)])],
        [bed.write(bed.subject("A"), [_set("n", 2)]), bed.write(bed.subject("B"), [_set("n", 3)])],
    )
    await bed.fail(held_by)
    outcome = await apply_row(bed.svc, newer)
    assert outcome.outcome == "held"
    assert outcome.held_by == held_by
    assert await bed.status(newer) == "pending"
    assert await execute("SELECT data FROM state_records WHERE state = %s", (bed.state,)) == []


async def test_a_chain_behind_a_failed_row_is_held_by_the_failed_row(bed: OutboxBed) -> None:
    failed, middle, last = await bed.enqueue_together(
        [bed.write(bed.subject("A"), [_set("n", 1)])],
        [bed.write(bed.subject("A"), [_set("n", 2)]), bed.write(bed.subject("B"), [_set("n", 2)])],
        [bed.write(bed.subject("B"), [_set("n", 3)])],
    )
    await bed.fail(failed)
    outcome = await apply_row(bed.svc, last)
    assert (outcome.outcome, outcome.held_by) == ("held", failed)
    assert await bed.status(middle) == "pending"
    assert await bed.status(last) == "pending"


def test_transient_failures_are_the_timeouts_the_unavailable_and_lost_connections() -> None:
    class _KindError(Exception):
        def __init__(self, kind: ErrorKind) -> None:
            super().__init__("x")
            self.__tai_error_kind__ = kind

    assert is_transient(AdminShutdown("gone"))
    assert is_transient(_KindError(ErrorKind.UNAVAILABLE))
    assert is_transient(_KindError(ErrorKind.TIMED_OUT))
    assert not is_transient(ValueValidationError("refused"))
    assert not is_transient(_KindError(ErrorKind.CONFLICT))


async def test_a_transient_failure_backs_off_and_the_last_attempt_fails_the_row(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_MAX_ATTEMPTS="2", STATES_OUTBOX_RETRY_BASE_SECONDS="10")
    row = await bed.enqueue(bed.write(bed.subject(), [_set("n", 1)]))

    async def _lost(*args: Any, **kwargs: Any) -> Any:
        raise AdminShutdown("connection lost")

    monkeypatch.setattr(bed.svc, "_commit_writes", _lost)
    assert (await apply_row(bed.svc, row)).outcome == "retry"
    (attempts, delay, status, last_error) = (
        await execute(
            "SELECT attempts, extract(epoch FROM next_attempt_at - now())::float8, status, last_error "
            "FROM state_outbox WHERE id = %s",
            (row,),
        )
    )[0]
    assert (attempts, status) == (1, "pending")
    assert 8 < delay <= 10
    assert "connection lost" in last_error  # the pooled client reports the lost connection
    assert (await apply_row(bed.svc, row)).outcome == "failed"
    assert (await execute("SELECT status, failed_phase, attempts FROM state_outbox WHERE id = %s", (row,)))[0] == (
        "failed",
        "records",
        2,
    )


async def test_a_deterministic_failure_fails_the_row_loudly_on_every_channel(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    notified: list[str] = []
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def _notify(message: str, *, save_id: int) -> None:
        notified.append(message)

    class _Hooks:
        async def on_event(self, *, topic: str, payload: dict[str, Any]) -> None:
            emitted.append((topic, payload))

    monkeypatch.setattr(loud_mod, "notify_operators", _notify)
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: _Hooks())
    failed_before = outbox_metrics().failed.labels("records")._value.get()
    row = await bed.enqueue_refused()
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.states.outbox.loud"):
        outcome = await apply_row(bed.svc, row)
    assert outcome.outcome == "failed"
    assert f"pending save {row} failed in its records phase" in caplog.text
    last_error = (await execute("SELECT last_error FROM state_outbox WHERE id = %s", (row,)))[0][0]
    assert notified == [
        "A pending state save failed; its subjects are held until it is retried or discarded. "
        + f"Save {row}, run -, phase records: {last_error}"
    ]
    assert [topic for topic, _payload in emitted] == ["states_outbox_save_failed"]
    payload = emitted[0][1]
    assert payload["save_id"] == str(row)
    assert payload["phase"] == "records"
    assert payload["error_kind"] == "bad_input"
    assert outbox_metrics().failed.labels("records")._value.get() == failed_before + 1


async def test_a_reader_meeting_a_failing_save_on_the_serving_loop_is_refused_before_the_hooks_run(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    emitted: list[str] = []

    class _GatedHooks:
        async def on_event(self, *, topic: str, payload: dict[str, Any]) -> None:
            await gate.wait()
            emitted.append(topic)

    async def _notify(message: str, *, save_id: int) -> None:
        return None

    monkeypatch.setattr(loud_mod, "notify_operators", _notify)
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: _GatedHooks())
    monkeypatch.setattr(apply_mod, "on_serving_loop", lambda: True)
    row = await bed.enqueue_refused()
    with pytest.raises(StatePendingSaveFailedError, match=f"has a failed pending save {row}"):
        await bed.svc.read(bed.state, bed.subject())
    assert emitted == []  # the hook fan-out runs in its own root task, never on the reader's wait
    gate.set()
    await asyncio.gather(*in_flight_tasks())
    assert emitted == ["states_outbox_save_failed"]


async def test_off_the_serving_loop_the_failure_event_is_emitted_inline(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[str] = []

    class _Hooks:
        async def on_event(self, *, topic: str, payload: dict[str, Any]) -> None:
            emitted.append(topic)

    async def _notify(message: str, *, save_id: int) -> None:
        return None

    monkeypatch.setattr(loud_mod, "notify_operators", _notify)
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: _Hooks())
    monkeypatch.setattr(apply_mod, "on_serving_loop", lambda: False)
    row = await bed.enqueue_refused()
    assert (await apply_row(bed.svc, row)).outcome == "failed"
    assert emitted == ["states_outbox_save_failed"]


async def test_a_divergence_is_logged_counted_and_emitted_on_the_runs_trace(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[dict[str, Any]] = []

    class _Writer:
        def current_trace_id(self) -> str | None:
            return "trace-1"

        def create_event(self, **kwargs: Any) -> None:
            events.append(kwargs)

    class _Monitoring:
        writer = _Writer()

    monkeypatch.setattr("tai42_skeleton.monitoring.get_monitoring", lambda: _Monitoring())
    before = outbox_metrics().divergences._value.get()
    row = await bed.enqueue(
        bed.write(bed.subject(), [{"op": "set", "path": ["n"], "value": 1, "guard": {"path": ["n"], "expected": None}}])
    )
    # Another writer sets n first (straight into the table: a facet write would apply this save first).
    await execute(
        "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data) "
        "VALUES (%s, 'agent', 'a', 'thread', 't1', '{\"n\": 5}'::jsonb)",
        (bed.state,),
    )
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.states.outbox.apply"):
        assert (await apply_row(bed.svc, row)).outcome == "applied"
    assert f"pending save {row} diverged from its projection" in caplog.text
    assert outbox_metrics().divergences._value.get() == before + 1
    assert [e["name"] for e in events] == ["state:pending-save-divergence"]
    assert events[0]["trace_context"].trace_id == "trace-1"
    assert events[0]["output"]["outbox_id"] == str(row)
