"""Deferred calls on real Postgres: the claim, the lease, crash-resume, the sweep, shutdown, and who runs them."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from tai42_contract.states.models import StateContext, SubjectCandidates

from tai42_skeleton.states.context import state_context
from tai42_skeleton.states.outbox import apply as apply_mod
from tai42_skeleton.states.outbox import enqueue as enqueue_mod
from tai42_skeleton.states.outbox import sweep as sweep_mod
from tai42_skeleton.states.outbox.apply import in_flight_tasks, run_calls, spawn_tracked
from tai42_skeleton.states.outbox.drain import current_applying_save

from .conftest import OutboxBed, ProbeKind, execute

pytestmark = pytest.mark.integration


def _ctx(key: str = "A") -> StateContext:
    return StateContext(
        door="api", candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": key})
    )


async def _calls_row(bed: OutboxBed, *calls: tuple[str, dict[str, Any]]) -> int:
    with state_context(_ctx()):
        return await bed.enqueue(calls=calls)


async def test_the_probe_kind_receives_its_payload_and_a_stable_key_per_call(bed: OutboxBed, probe: ProbeKind) -> None:
    row = await _calls_row(bed, ("first", {"x": 1}), ("second", {"x": 2}))
    scopes: list[Any] = []
    real_apply = probe.apply

    async def _apply(payload: dict[str, Any], *, idempotency_key: str) -> None:
        scopes.append(current_applying_save())
        await real_apply(payload, idempotency_key=idempotency_key)

    probe.apply = _apply  # type: ignore[method-assign]
    await run_calls(bed.svc, row)
    assert probe.applied == [
        ({"target": "first", "arguments": {"x": 1}}, f"{row}:0"),
        ({"target": "second", "arguments": {"x": 2}}, f"{row}:1"),
    ]
    assert [s.save_id for s in scopes] == [row, row]
    assert current_applying_save() is None
    assert await bed.status(row) is None


async def test_a_claim_waits_for_an_older_outstanding_save_on_its_subjects(bed: OutboxBed, probe: ProbeKind) -> None:
    older = await bed.enqueue(bed.write(bed.subject("A"), [{"op": "set", "path": ["n"], "value": 1}]))
    row = await _calls_row(bed, ("echo", {}))
    await run_calls(bed.svc, row)
    assert probe.applied == []
    assert await bed.status(row) == "calls"
    assert await bed.status(older) == "pending"


async def test_a_failing_call_backs_off_then_fails_the_row_in_its_calls_phase(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_contract.states.errors import DeferredCallRefusedError

    from tai42_skeleton.states.outbox import loud as loud_mod

    async def _notify(message: str, *, save_id: int) -> None:
        return None

    monkeypatch.setattr(loud_mod, "notify_operators", _notify)
    probe.raises = DeferredCallRefusedError("refused")
    row = await _calls_row(bed, ("echo", {}))
    await run_calls(bed.svc, row)
    status, phase, error, done = (
        await execute("SELECT status, failed_phase, last_error, calls_done FROM state_outbox WHERE id = %s", (row,))
    )[0]
    assert (status, phase, done) == ("failed", "calls", 0)
    assert error == "DeferredCallRefusedError: refused"


@pytest.mark.parametrize("resumable", [True, False])
async def test_a_call_a_lapsed_claim_interrupted_is_re_run_only_when_resumable(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch, resumable: bool
) -> None:
    from tai42_skeleton.states.outbox import loud as loud_mod

    async def _notify(message: str, *, save_id: int) -> None:
        return None

    monkeypatch.setattr(loud_mod, "notify_operators", _notify)
    probe.resume = resumable
    row = await _calls_row(bed, ("echo", {}))
    await execute(
        "UPDATE state_outbox SET status = 'running', claimed_by = 'dead', lease_until = now() - interval '1 second' "
        "WHERE id = %s",
        (row,),
    )
    await run_calls(bed.svc, row)
    if resumable:
        assert probe.applied == [({"target": "echo", "arguments": {}}, f"{row}:0")]
        assert await bed.status(row) is None
    else:
        assert probe.applied == []
        status, error = (await execute("SELECT status, last_error FROM state_outbox WHERE id = %s", (row,)))[0]
        assert status == "failed"
        assert (
            error == "deferred call 'echo' was interrupted by a process exit and its tool does not declare crash-resume"
        )


async def test_the_heartbeat_extends_the_lease_while_the_call_runs(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STATES_OUTBOX_CLAIM_LEASE_SECONDS", "0.6")
    probe.gate = asyncio.Event()
    row = await _calls_row(bed, ("echo", {}))
    runner = asyncio.create_task(run_calls(bed.svc, row))
    while not probe.applied:
        await asyncio.sleep(0.02)
    first = (await execute("SELECT lease_until FROM state_outbox WHERE id = %s", (row,)))[0][0]
    await asyncio.sleep(0.5)
    second = (await execute("SELECT lease_until FROM state_outbox WHERE id = %s", (row,)))[0][0]
    assert second > first
    probe.gate.set()
    await runner
    assert await bed.status(row) is None


async def test_a_claim_taken_over_cancels_the_running_call(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("STATES_OUTBOX_CLAIM_LEASE_SECONDS", "0.3")
    probe.gate = asyncio.Event()
    row = await _calls_row(bed, ("echo", {}))
    runner = asyncio.create_task(run_calls(bed.svc, row))
    while not probe.applied:
        await asyncio.sleep(0.02)
    await execute("UPDATE state_outbox SET claimed_by = 'other' WHERE id = %s", (row,))
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.states.outbox.apply"):
        await asyncio.wait_for(runner, 5)
    assert f"pending save {row} lost its calls claim" in caplog.text
    assert (await execute("SELECT status, claimed_by FROM state_outbox WHERE id = %s", (row,)))[0] == (
        "running",
        "other",
    )


async def test_the_sweep_recovers_a_pending_save_and_a_lapsed_claim(bed: OutboxBed, probe: ProbeKind) -> None:
    pending = await bed.enqueue(bed.write(bed.subject("P"), [{"op": "set", "path": ["n"], "value": 1}]))
    probe.resume = True
    lapsed = await _calls_row(bed, ("echo", {}))
    await execute(
        "UPDATE state_outbox SET status = 'running', claimed_by = 'dead', lease_until = now() - interval '1 second' "
        "WHERE id = %s",
        (lapsed,),
    )
    await sweep_mod.sweep_pass()
    assert await bed.status(pending) is None
    assert await bed.status(lapsed) is None
    assert probe.applied == [({"target": "echo", "arguments": {}}, f"{lapsed}:0")]


async def test_shutdown_awaits_the_in_flight_applies_up_to_the_grace_then_names_the_cancelled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("STATES_OUTBOX_SHUTDOWN_GRACE_SECONDS", "0.2")
    done = asyncio.Event()

    async def _quick() -> None:
        done.set()

    async def _stuck() -> None:
        await asyncio.Event().wait()

    spawn_tracked(_quick(), name="tai-states-outbox-1")
    spawn_tracked(_stuck(), name="tai-states-outbox-2")
    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.states.outbox.sweep"):
        await sweep_mod.stop_state_outbox_sweep()
    assert done.is_set()
    assert "shutdown cancelled 1 in-flight pending-save task(s)" in caplog.text
    assert "tai-states-outbox-2" in caplog.text
    assert in_flight_tasks() == set()


async def test_off_the_serving_loop_the_records_apply_inline_and_the_calls_wait_for_the_sweep(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(enqueue_mod, "dispatch_pending_save", _REAL_DISPATCH)
    monkeypatch.setattr(apply_mod, "on_serving_loop", lambda: False)
    monkeypatch.setattr(enqueue_mod, "on_serving_loop", lambda: False)
    with state_context(_ctx()):
        row = await bed.enqueue(
            bed.write(bed.subject("A"), [{"op": "set", "path": ["n"], "value": 1}]), calls=(("echo", {}),)
        )
    assert await bed.status(row) == "calls"
    assert probe.applied == []
    record = await bed.svc.read(bed.state, bed.subject("A"))
    assert record is not None
    assert record.data == {"n": 1}
    await sweep_mod.sweep_pass()
    assert probe.applied == [({"target": "echo", "arguments": {}}, f"{row}:0")]
    assert await bed.status(row) is None


async def test_on_the_serving_loop_a_readers_help_along_never_waits_for_the_calls(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(enqueue_mod, "dispatch_pending_save", _REAL_DISPATCH)
    monkeypatch.setattr(apply_mod, "on_serving_loop", lambda: True)
    monkeypatch.setattr(enqueue_mod, "on_serving_loop", lambda: True)
    probe.gate = asyncio.Event()
    real_apply_row = apply_mod.apply_row
    applied_by: list[str] = []

    async def _slow_fast_path(service: Any, row_id: int, *, deadline: float | None = None) -> Any:
        if deadline is None:  # the fast path's root task: let the reader's help-along win the record part
            await asyncio.sleep(0.2)
        outcome = await real_apply_row(service, row_id, deadline=deadline)
        applied_by.append(f"{'reader' if deadline is not None else 'fast'}:{outcome.outcome}")
        return outcome

    monkeypatch.setattr(apply_mod, "apply_row", _slow_fast_path)
    with state_context(_ctx()):
        row = await bed.enqueue(
            bed.write(bed.subject("A"), [{"op": "set", "path": ["n"], "value": 1}]), calls=(("echo", {}),)
        )
    from tai42_skeleton.states.outbox import drain as drain_mod

    monkeypatch.setattr(drain_mod, "apply_row", _slow_fast_path)
    record = await bed.svc.read(bed.state, bed.subject("A"))
    assert record is not None
    assert record.data == {"n": 1}
    assert applied_by[0] == "reader:applied"
    assert probe.applied == []  # the call has not run on the reader's wait
    while not probe.applied:
        await asyncio.sleep(0.02)
    assert probe.applied == [({"target": "echo", "arguments": {}}, f"{row}:0")]
    probe.gate.set()
    await asyncio.gather(*in_flight_tasks())
    assert await bed.status(row) is None


async def test_the_retry_door_re_runs_the_calls_through_the_fast_path(
    bed: OutboxBed, probe: ProbeKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apply_mod, "on_serving_loop", lambda: True)
    from tai42_skeleton.states.service import pending_saves as pending_mod

    monkeypatch.setattr(pending_mod, "on_serving_loop", lambda: True)
    row = await _calls_row(bed, ("echo", {}))
    await execute(
        "UPDATE state_outbox SET status = 'failed', failed_phase = 'calls', failed_at = now(), "
        "last_error = 'X: boom' WHERE id = %s",
        (row,),
    )
    outcome = await bed.svc.retry_pending_save(row)
    assert outcome.requeued is True
    assert outcome.row is not None
    assert outcome.row.status == "calls"
    await asyncio.gather(*in_flight_tasks())
    assert probe.applied == [({"target": "echo", "arguments": {}}, f"{row}:0")]
    assert await bed.status(row) is None


_REAL_DISPATCH = enqueue_mod.dispatch_pending_save
