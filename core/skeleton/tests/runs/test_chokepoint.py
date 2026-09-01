"""The runs-index lifecycle chokepoint: the START/terminal write pair, the outcome
decision (success / error / parked / aborted), trace-id capture + backfill, the
lifecycle-correlation ``interaction_id`` (a park's sentinel id at the terminal write,
the ambient resume origin at START), attribution capture, and the two safety postures
(store OFF, and a store failure never breaking the run).

Drives ``record_outermost_preset_run`` directly with a spy store — the binding gate
that decides WHICH dispatches enter it (outermost-only, raw tools excluded) is covered
by ``tests/presets/test_run_index_chokepoint.py`` through the real ``run_tool``.
"""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.monitoring import RunAttribution

import tai42_skeleton.runs.chokepoint as chokepoint
from tai42_skeleton.runs.chokepoint import record_outermost_preset_run, resume_origin
from tai42_skeleton.tools.attribution import run_attribution


class _SpyStore:
    def __init__(self, *, start_error: bool = False, terminal_error: bool = False) -> None:
        self.starts: list[dict] = []
        self.terminals: list[dict] = []
        self._start_error = start_error
        self._terminal_error = terminal_error

    async def insert_start(
        self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
    ):
        if self._start_error:
            raise RuntimeError("db down")
        self.starts.append(
            {
                "run_id": run_id,
                "preset_name": preset_name,
                "preset_version": preset_version,
                "trace_id": trace_id,
                "user_id": user_id,
                "session_id": session_id,
                "interaction_id": interaction_id,
                "started_at": started_at,
            }
        )

    async def update_outcome(self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None):
        # Records the attempt BEFORE raising (unlike insert_start), so failure-path
        # tests can assert the terminal write was reached — not that it succeeded.
        self.terminals.append(
            {
                "run_id": run_id,
                "outcome": outcome,
                "ended_at": ended_at,
                "trace_id": trace_id,
                "interaction_id": interaction_id,
            }
        )
        if self._terminal_error:
            raise ConnectionError("db down at terminal write")


@pytest.fixture
def wire(monkeypatch):
    """Install a spy store + a chosen trace-id sample, with the store forced ON."""

    def _install(*, store: _SpyStore, trace_id: str | None = None, configured: bool = True):
        monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: configured)
        monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: store)
        monkeypatch.setattr(chokepoint, "_safe_trace_id", lambda: trace_id)
        return store

    return _install


async def test_success_records_start_and_terminal(wire):
    store = wire(store=_SpyStore(), trace_id="trace-1")
    async with record_outermost_preset_run("weather", 4) as run:
        run.observe("a plain result")
    assert len(store.starts) == 1
    assert store.starts[0]["preset_name"] == "weather"
    assert store.starts[0]["preset_version"] == 4
    assert store.starts[0]["trace_id"] == "trace-1"
    assert len(store.terminals) == 1
    assert store.terminals[0]["outcome"] == "success"
    assert store.terminals[0]["run_id"] == store.starts[0]["run_id"]


async def test_attribution_identity_is_captured(wire):
    store = wire(store=_SpyStore())
    with run_attribution(RunAttribution(user_id="person-7", session_id="thread-3")):
        async with record_outermost_preset_run("wx", 1) as run:
            run.observe("ok")
    assert store.starts[0]["user_id"] == "person-7"
    assert store.starts[0]["session_id"] == "thread-3"


async def test_parked_result_records_parked(wire):
    store = wire(store=_SpyStore())
    parked = SuspendedInteraction(interaction_id="i1", expiry_at=None)
    async with record_outermost_preset_run("wx", 1) as run:
        run.observe(parked)
    assert store.terminals[0]["outcome"] == "parked"


async def test_exception_records_error_and_propagates(wire):
    store = wire(store=_SpyStore())
    with pytest.raises(ValueError, match="boom"):
        async with record_outermost_preset_run("wx", 1):
            raise ValueError("boom")
    assert store.terminals[0]["outcome"] == "error"


async def test_cancellation_records_aborted_and_propagates(wire):
    # Cancelled is NOT failed: a ``CancelledError`` escaping the body records the
    # distinct terminal ``aborted`` — and the cancellation still propagates untouched.
    store = wire(store=_SpyStore())
    with pytest.raises(asyncio.CancelledError):
        async with record_outermost_preset_run("wx", 1):
            raise asyncio.CancelledError()
    assert store.terminals[0]["outcome"] == "aborted"


async def test_real_task_cancellation_records_aborted_then_cancels(wire):
    # A REAL ``task.cancel()`` mid-body: the terminal write is a best-effort
    # unshielded attempt during the unwind (the drain idiom — one cancel, then wait),
    # so the spy still sees ``aborted`` and the task ends cancelled.
    store = wire(store=_SpyStore())

    async def body() -> None:
        async with record_outermost_preset_run("wx", 1):
            await asyncio.sleep(60)

    task = asyncio.create_task(body())
    await asyncio.sleep(0)  # let the START write land and the body reach the sleep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.terminals[0]["outcome"] == "aborted"


async def test_parked_terminal_write_carries_the_interaction_id(wire):
    # The park's ``SuspendedInteraction`` sentinel carries the lifecycle key: the
    # parked row's terminal write records it so the later resume row is joinable.
    store = wire(store=_SpyStore())
    async with record_outermost_preset_run("wx", 1) as run:
        run.observe(SuspendedInteraction(interaction_id="i-park", expiry_at=None))
    assert store.terminals[0]["outcome"] == "parked"
    assert store.terminals[0]["interaction_id"] == "i-park"


async def test_resume_origin_is_recorded_at_start(wire):
    # A dispatch fired under the ambient resume-origin deposit (the continuation
    # drive's re-entry) records the origin interaction id on its START row — the
    # link back to the lifecycle it continues.
    store = wire(store=_SpyStore())
    with resume_origin("i-origin"):
        async with record_outermost_preset_run("wx", 1) as run:
            run.observe("ok")
    assert store.starts[0]["interaction_id"] == "i-origin"


async def test_no_correlation_available_is_null_never_an_error(wire):
    # Fail-safe: a plain dispatch (no park, no resume origin) carries NULL on both
    # writes — absence of a correlation is never an error.
    store = wire(store=_SpyStore())
    async with record_outermost_preset_run("wx", 1) as run:
        run.observe("ok")
    assert store.starts[0]["interaction_id"] is None
    assert store.terminals[0]["interaction_id"] is None


async def test_trace_id_backfilled_when_absent_at_start(wire, monkeypatch):
    # No trace at start, a trace at the terminal write: the id is backfilled.
    store = _SpyStore()
    samples = iter([None, "trace-late"])
    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: store)
    monkeypatch.setattr(chokepoint, "_safe_trace_id", lambda: next(samples))
    async with record_outermost_preset_run("wx", 1) as run:
        run.observe("ok")
    assert store.starts[0]["trace_id"] is None
    assert store.terminals[0]["trace_id"] == "trace-late"


async def test_store_off_is_a_clean_no_op(wire):
    store = wire(store=_SpyStore(), configured=False)
    async with record_outermost_preset_run("wx", 1) as run:
        run.observe("ok")
    assert store.starts == []
    assert store.terminals == []


async def test_start_failure_never_breaks_the_run(wire):
    # A failed START write degrades to an unrecorded run — the body still runs, no raise,
    # and no terminal write is attempted for a run that was never recorded.
    store = wire(store=_SpyStore(start_error=True))
    ran = False
    async with record_outermost_preset_run("wx", 1) as run:
        ran = True
        run.observe("ok")
    assert ran is True
    assert store.terminals == []


async def test_start_failure_does_not_pollute_a_dispatch_error_context(wire):
    # A swallowed START error must NOT leak onto a dispatch exception's ``__context__``
    # (a yield-inside-except would chain it). The run's error must stay clean — the
    # secret-redaction invariants depend on it.
    wire(store=_SpyStore(start_error=True))
    with pytest.raises(ValueError, match="body boom") as exc_info:
        async with record_outermost_preset_run("wx", 1):
            raise ValueError("body boom")
    assert exc_info.value.__context__ is None


async def test_terminal_write_failure_never_breaks_a_successful_run(wire):
    # A store/DB error on the terminal write is logged and swallowed: the CM exits
    # cleanly and the caller still gets the body's result — enumeration never breaks a run.
    store = wire(store=_SpyStore(terminal_error=True))
    result = None
    async with record_outermost_preset_run("wx", 1) as run:
        run.observe("the body result")
        result = "the body result"
    # No exception escaped the CM; the terminal write was attempted (and swallowed).
    assert result == "the body result"
    assert len(store.terminals) == 1


async def test_terminal_write_failure_does_not_mask_a_body_error(wire):
    # When the body raises AND the terminal write also raises, the ORIGINAL body error
    # propagates unchanged — the swallowed store error must not replace it or pollute
    # its ``__context__`` (the secret-redaction invariants depend on a clean error).
    wire(store=_SpyStore(terminal_error=True))
    with pytest.raises(ValueError, match="body boom") as exc_info:
        async with record_outermost_preset_run("wx", 1):
            raise ValueError("body boom")
    assert type(exc_info.value) is ValueError
    assert exc_info.value.__context__ is None
