"""The fork/rebuild barrier: mutual exclusion, writer preference, re-entrancy, the
async façade's single-thread ownership, and the bounded-and-loud timeout branches.

Waits are ``Event`` rendezvous wherever a rendezvous exists: a positive assertion
waits for the event with a generous budget, a negative assertion ("this must NOT
proceed") waits a short budget and asserts the event stayed clear. One condition has no
event to wait on — a gate flag another thread flips — and polls on a small sleep instead.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from tai42_kit.fork_gate import ForkGate, fork_gate

# Budget for an event that MUST fire (generous: only a real hang exceeds it).
_ARRIVES = 5.0
# Budget for an event that must NOT fire (short: proves non-progress cheaply).
_STAYS_CLEAR = 0.2
# The timeout a test drives into an intentionally-exceeded wait.
_TINY = 0.05
# Poll interval where no event exists to wait on.
_POLL = 0.005
# A budget an abortable acquire must beat by a wide margin: a cancellation that unwinds
# promptly finishes in milliseconds, one that waits out its budget takes _SLOW_BUDGET.
_SLOW_BUDGET = 30.0
_PROMPT = 3.0


@pytest.fixture
def gate() -> Iterator[ForkGate]:
    """A private gate per test — the module singleton is process-wide state.

    A guard a test installs stays registered with the interpreter's fork hooks (they
    cannot be undone), so the fixture drops it: the hooks of an unguarded gate do nothing.
    """
    private = ForkGate()
    yield private
    private._fork_hooks.guard = None


@pytest.fixture
def joined() -> Iterator[list[threading.Thread]]:
    """Threads a test starts, joined before it ends so none outlives the test."""
    threads: list[threading.Thread] = []
    yield threads
    for thread in threads:
        thread.join(timeout=_ARRIVES)
        assert not thread.is_alive(), "a helper thread outlived its test"


def _spawn(joined: list[threading.Thread], fn: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    joined.append(thread)
    thread.start()
    return thread


def _poll_until(predicate: Callable[[], bool], budget: float) -> bool:
    """Wait for a gate flag another thread flips — the one condition with no event to
    rendezvous on. Returns whether it became true within ``budget``."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL)
    return predicate()


def test_singleton_is_a_fork_gate():
    assert isinstance(fork_gate, ForkGate)


def test_idle_gate_admits_both_sides(gate: ForkGate):
    assert not gate.blocked
    assert gate.live_spans == 0
    with gate.job_span(timeout=_ARRIVES):
        assert gate.live_spans == 1
    assert gate.live_spans == 0
    with gate.exclusive(timeout=_ARRIVES):
        assert gate.blocked
    assert not gate.blocked


def test_job_span_waits_while_a_rebuild_is_held(gate: ForkGate, joined: list[threading.Thread]):
    held = threading.Event()
    release = threading.Event()
    entered = threading.Event()

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            held.set()
            release.wait(_ARRIVES)

    _spawn(joined, rebuild)
    assert held.wait(_ARRIVES)

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            entered.set()

    _spawn(joined, job)
    # The rebuild is held, so the span must not enter.
    assert not entered.wait(_STAYS_CLEAR)
    assert gate.live_spans == 0

    release.set()
    assert entered.wait(_ARRIVES)


def test_rebuild_waits_for_open_job_spans_to_drain(gate: ForkGate, joined: list[threading.Thread]):
    span_open = threading.Event()
    close_span = threading.Event()
    rebuilt = threading.Event()

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            span_open.set()
            close_span.wait(_ARRIVES)

    _spawn(joined, job)
    assert span_open.wait(_ARRIVES)

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            rebuilt.set()

    _spawn(joined, rebuild)
    # A live child may still be importing, so the re-import must not start.
    assert not rebuilt.wait(_STAYS_CLEAR)
    # ...but the intent is registered, so the gate already reads as blocked.
    assert gate.blocked

    close_span.set()
    assert rebuilt.wait(_ARRIVES)


def test_a_pending_rebuild_is_not_starved_by_back_to_back_spans(gate: ForkGate, joined: list[threading.Thread]):
    """Writer preference: once a rebuild is pending, the next span queues behind
    it rather than renewing the block."""
    first_open = threading.Event()
    close_first = threading.Event()
    rebuild_pending = threading.Event()
    rebuilt = threading.Event()
    second_entered = threading.Event()

    def first_job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            first_open.set()
            close_first.wait(_ARRIVES)

    _spawn(joined, first_job)
    assert first_open.wait(_ARRIVES)

    def rebuild() -> None:
        rebuild_pending.set()
        with gate.exclusive(timeout=_ARRIVES):
            rebuilt.set()

    _spawn(joined, rebuild)
    assert rebuild_pending.wait(_ARRIVES)
    # ``blocked`` flips inside ``exclusive`` before it starts waiting, so it is the
    # observable proof the intent is registered — the ordering the second span must
    # queue behind. No event marks that instant, so poll it on a small sleep, bounded so
    # a gate that never registers fails instead of hanging.
    assert _poll_until(lambda: gate.blocked, _ARRIVES)

    def second_job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            second_entered.set()

    _spawn(joined, second_job)
    # The second span must yield to the pending rebuild, not extend the drain.
    assert not second_entered.wait(_STAYS_CLEAR)

    close_first.set()
    assert rebuilt.wait(_ARRIVES)
    assert second_entered.wait(_ARRIVES)


def test_two_rebuilds_serialize(gate: ForkGate, joined: list[threading.Thread]):
    first_held = threading.Event()
    release_first = threading.Event()
    second_held = threading.Event()

    def first() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            first_held.set()
            release_first.wait(_ARRIVES)

    def second() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            second_held.set()

    _spawn(joined, first)
    assert first_held.wait(_ARRIVES)
    _spawn(joined, second)
    assert not second_held.wait(_STAYS_CLEAR)

    release_first.set()
    assert second_held.wait(_ARRIVES)


def test_exclusive_is_reentrant_on_the_owning_thread(gate: ForkGate):
    """Only the OUTERMOST call holds; every nested exit leaves the hold intact, at
    any depth. ``_TINY`` on the nested calls proves they never wait."""
    with gate.exclusive(timeout=_ARRIVES):
        with gate.exclusive(timeout=_TINY):
            with gate.exclusive(timeout=_TINY):
                assert gate.blocked
            assert gate.blocked
        assert gate.blocked
    assert not gate.blocked


def test_a_nested_exclusive_still_excludes_other_threads(gate: ForkGate, joined: list[threading.Thread]):
    """The nested hold is not a release: a job span on another thread stays queued
    until the outermost call exits."""
    nested = threading.Event()
    release = threading.Event()
    entered = threading.Event()

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES), gate.exclusive(timeout=_TINY):
            nested.set()
            release.wait(_ARRIVES)

    _spawn(joined, rebuild)
    assert nested.wait(_ARRIVES)

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            entered.set()

    _spawn(joined, job)
    assert not entered.wait(_STAYS_CLEAR)

    release.set()
    assert entered.wait(_ARRIVES)


def _hold_rebuild(gate: ForkGate, joined: list[threading.Thread]) -> threading.Event:
    """Hold ``exclusive`` on a helper thread until the returned event is set."""
    held = threading.Event()
    release = threading.Event()

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            held.set()
            release.wait(_ARRIVES)

    _spawn(joined, rebuild)
    assert held.wait(_ARRIVES)
    return release


def _release_on_tick(release: threading.Event, ticks: list[float], count: int) -> Callable[[], None]:
    """A tick that records its time and ends the held rebuild on its ``count``-th call."""

    def tick() -> None:
        ticks.append(time.monotonic())
        if len(ticks) == count:
            release.set()

    return tick


def test_a_waiting_job_span_ticks_until_the_rebuild_releases(gate: ForkGate, joined: list[threading.Thread]):
    ticks: list[float] = []
    entered_at: list[float] = []
    release = _hold_rebuild(gate, joined)

    with gate.job_span(timeout=_ARRIVES, tick=_release_on_tick(release, ticks, 3), tick_every=_TINY):
        entered_at.append(time.monotonic())

    assert len(ticks) == 3
    assert all(at <= entered_at[0] for at in ticks)


def test_an_uncontended_job_span_never_ticks(gate: ForkGate):
    ticks: list[None] = []

    with gate.job_span(timeout=_ARRIVES, tick=lambda: ticks.append(None), tick_every=_TINY):
        assert gate.live_spans == 1

    assert ticks == []


def test_a_job_span_that_overruns_its_wait_still_ticks_meanwhile(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    ticks: list[None] = []
    release = _hold_rebuild(gate, joined)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        with gate.job_span(timeout=0.3, tick=lambda: ticks.append(None), tick_every=_TINY):
            assert gate.live_spans == 1
        assert "could not enter within 0.3s" in caplog.text

    assert len(ticks) >= 3
    release.set()


def test_a_raising_tick_is_logged_once_and_the_wait_goes_on(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    calls: list[None] = []
    release = _hold_rebuild(gate, joined)

    def tick() -> None:
        calls.append(None)
        # The wait goes on for many more tick periods after this one fails.
        threading.Timer(0.3, release.set).start()
        raise OSError("the report cannot be written")

    with (
        caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"),
        gate.job_span(timeout=_ARRIVES, tick=tick, tick_every=_TINY),
    ):
        assert gate.live_spans == 1

    assert calls == [None]
    assert [record.getMessage() for record in caplog.records] == [
        "fork gate: the progress tick during a job span's wait failed; this wait reports no more progress"
    ]


@pytest.mark.parametrize("tick_every", [0.0, -1.0, float("inf"), float("nan")])
def test_a_non_positive_tick_period_is_refused(gate: ForkGate, tick_every: float):
    with (
        pytest.raises(ValueError, match="tick_every must be positive seconds"),
        gate.job_span(timeout=_ARRIVES, tick=lambda: None, tick_every=tick_every),
    ):
        pass
    assert gate.live_spans == 0


def test_a_job_span_is_reentrant_on_its_own_thread(gate: ForkGate, joined: list[threading.Thread]):
    """A nested span enters at once even behind a pending rebuild, and only the outer
    exit releases the one span the rebuild waits for."""
    rebuilt = threading.Event()
    with gate.job_span(timeout=_ARRIVES):

        def rebuild() -> None:
            with gate.exclusive(timeout=_ARRIVES):
                rebuilt.set()

        _spawn(joined, rebuild)
        assert _poll_until(lambda: gate.blocked, _ARRIVES)
        started = time.monotonic()
        with gate.job_span(timeout=_ARRIVES):
            assert time.monotonic() - started < _STAYS_CLEAR
            assert gate.live_spans == 1
        assert gate.live_spans == 1
        assert not rebuilt.wait(_STAYS_CLEAR)
    assert rebuilt.wait(_ARRIVES)
    assert gate.live_spans == 0


def test_a_nested_job_span_on_another_thread_still_waits(gate: ForkGate, joined: list[threading.Thread]):
    """A thread started without the job's context carries no mark of its span: its span is its own and waits."""
    entered = threading.Event()
    release = _hold_rebuild(gate, joined)

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            entered.set()

    with gate.job_span(timeout=_TINY):
        _spawn(joined, job)
        assert not entered.wait(_STAYS_CLEAR)
    assert not entered.wait(_STAYS_CLEAR)
    release.set()
    assert entered.wait(_ARRIVES)


def test_a_span_left_after_a_reset_changes_nothing_in_the_reset_gate(gate: ForkGate):
    """A child forked from inside a span may leave it after its gate was reset: that
    span is the parent's, so the child's counters stay as the reset left them."""
    with gate.job_span(timeout=_ARRIVES):
        gate.reset_after_fork()
    assert gate.live_spans == 0
    with gate.exclusive(timeout=_TINY):
        assert gate.blocked


def test_a_raising_body_releases_its_hold(gate: ForkGate):
    with pytest.raises(RuntimeError, match="span blew up"), gate.job_span(timeout=_ARRIVES):
        raise RuntimeError("span blew up")
    assert gate.live_spans == 0

    with pytest.raises(RuntimeError, match="rebuild blew up"), gate.exclusive(timeout=_ARRIVES):
        raise RuntimeError("rebuild blew up")
    assert not gate.blocked


def test_a_job_span_that_cannot_enter_logs_and_proceeds(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    held = threading.Event()
    release = threading.Event()

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            held.set()
            release.wait(_ARRIVES)

    _spawn(joined, rebuild)
    assert held.wait(_ARRIVES)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        with gate.job_span(timeout=_TINY):
            # Proceeds despite the held rebuild, and still registers the span so a
            # later rebuild waits for this child.
            assert gate.live_spans == 1
        assert "could not enter" in caplog.text
        # The consequence named holds on every pool: an in-process job's and a fork's.
        assert (
            "a job run in the span may import against a half-torn module table, and a child "
            "forked in it may deadlock on an inherited import lock" in caplog.text
        )

    release.set()


def test_a_rebuild_that_cannot_quiesce_logs_and_proceeds(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    span_open = threading.Event()
    close_span = threading.Event()

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            span_open.set()
            close_span.wait(_ARRIVES)

    _spawn(joined, job)
    assert span_open.wait(_ARRIVES)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        with gate.exclusive(timeout=_TINY):
            assert gate.blocked
        assert "could not quiesce" in caplog.text
        # The report names what actually blocked, not a guess.
        assert "1 job span(s) still open" in caplog.text
        # The consequence named holds on every pool: an in-process job's and a fork's.
        assert (
            "a job still running during the re-import may import against a half-torn module "
            "table, and a forked child may deadlock on an inherited import lock" in caplog.text
        )

    close_span.set()


def test_reset_after_fork_drops_every_inherited_hold(gate: ForkGate):
    """A child forked from inside a job span inherits that span (and possibly a locked
    condition). The reset must leave a usable, empty gate — the child is a fresh
    single-threaded process, so nothing it inherited is its to release."""
    # The child's copy at the fork instant: its own span open, a rebuild registered by a
    # thread that does not exist post-fork, and the condition itself left locked. Set
    # directly: a lock left held by a thread that does not exist cannot be modelled with
    # a real ``exclusive``.
    gate._open_spans = {object()}
    gate._pending_rebuilds = 1
    gate._owner = 999999
    gate._cond.acquire()

    gate.reset_after_fork()

    assert gate.live_spans == 0
    assert not gate.blocked
    assert gate._owner is None
    # Usable: both sides acquire immediately, which a locked inherited condition or a
    # phantom span would prevent.
    with gate.job_span(timeout=_TINY):
        assert gate.live_spans == 1
    with gate.exclusive(timeout=_TINY):
        assert gate.blocked
    assert not gate.blocked
    assert gate.live_spans == 0


def test_reset_after_fork_installs_a_fresh_condition(gate: ForkGate):
    """The inherited condition may be locked by a thread that no longer exists, so the
    reset must REPLACE it, not merely unlock it."""
    before = gate._cond
    gate.reset_after_fork()
    assert gate._cond is not before


def test_an_original_holders_release_does_not_un_own_a_post_timeout_stealer(
    gate: ForkGate, joined: list[threading.Thread]
):
    """A timed-out rebuild STEALS ownership. When the original holder then finishes, its
    release must NOT clear ``_owner`` — that would un-own the stealer and let a job span
    fork straight into the stealer's live re-import."""
    original_held = threading.Event()
    finish_original = threading.Event()
    original_done = threading.Event()
    stealer_owns = threading.Event()
    finish_stealer = threading.Event()

    def original() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            original_held.set()
            finish_original.wait(_ARRIVES)
        original_done.set()

    def stealer() -> None:
        # Times out against the original's hold, logs, and proceeds — stealing ``_owner``.
        with gate.exclusive(timeout=_TINY):
            stealer_owns.set()
            finish_stealer.wait(_ARRIVES)

    _spawn(joined, original)
    assert original_held.wait(_ARRIVES)
    stealer_thread = _spawn(joined, stealer)
    assert stealer_owns.wait(_ARRIVES)
    assert gate._owner == stealer_thread.ident

    # The original now finishes, AFTER being stolen from.
    finish_original.set()
    assert original_done.wait(_ARRIVES)

    # Ownership must still be the stealer's, and the gate must still read as blocked
    # (the stealer's pending registration is outstanding), so no job span can enter.
    assert gate._owner == stealer_thread.ident
    assert gate.blocked
    entered = threading.Event()

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            entered.set()

    _spawn(joined, job)
    assert not entered.wait(_STAYS_CLEAR)

    finish_stealer.set()
    assert entered.wait(_ARRIVES)
    assert gate._owner is None
    assert not gate.blocked


async def test_exclusive_async_holds_and_releases_around_an_awaited_body(gate: ForkGate):
    async with gate.exclusive_async(timeout=_ARRIVES):
        assert gate.blocked
        # The body may await; the hold lives on its own thread, not this one.
        await asyncio.sleep(0)
        assert gate.blocked
    assert not gate.blocked


async def test_exclusive_async_releases_when_the_body_raises(gate: ForkGate):
    with pytest.raises(RuntimeError, match="async rebuild blew up"):
        async with gate.exclusive_async(timeout=_ARRIVES):
            raise RuntimeError("async rebuild blew up")
    assert not gate.blocked


async def test_exclusive_async_keeps_ownership_on_one_thread(gate: ForkGate):
    """Acquire and release must happen on the SAME thread, or ``_owner`` would name a
    thread that no longer holds the gate. The holder thread is neither this loop's
    thread nor a pooled ``to_thread`` worker."""
    async with gate.exclusive_async(timeout=_ARRIVES):
        owner = gate._owner
        assert owner is not None
        assert owner != threading.get_ident()
        assert owner != await asyncio.to_thread(threading.get_ident)
    assert gate._owner is None


async def test_exclusive_async_waits_for_an_open_job_span(gate: ForkGate, joined: list[threading.Thread]):
    span_open = threading.Event()
    close_span = threading.Event()

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            span_open.set()
            close_span.wait(_ARRIVES)

    _spawn(joined, job)
    assert span_open.wait(_ARRIVES)

    entered = asyncio.Event()

    async def rebuild() -> None:
        async with gate.exclusive_async(timeout=_ARRIVES):
            entered.set()

    task = asyncio.create_task(rebuild())
    # A live job child may still be importing, so the rebuild must not start — and the
    # loop must stay responsive while it waits.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(entered.wait()), timeout=_STAYS_CLEAR)
    assert gate.blocked

    close_span.set()
    await asyncio.wait_for(task, timeout=_ARRIVES)
    assert entered.is_set()
    assert not gate.blocked


async def test_exclusive_async_blocks_a_job_span_while_held(gate: ForkGate, joined: list[threading.Thread]):
    entered = threading.Event()

    async with gate.exclusive_async(timeout=_ARRIVES):

        def job() -> None:
            with gate.job_span(timeout=_ARRIVES):
                entered.set()

        _spawn(joined, job)
        assert not await asyncio.to_thread(entered.wait, _STAYS_CLEAR)

    assert await asyncio.to_thread(entered.wait, _ARRIVES)


async def test_a_cancel_during_the_acquire_unwinds_promptly_not_at_the_budget(
    gate: ForkGate, joined: list[threading.Thread]
):
    """A task cancelled WHILE the gate is being acquired must not strand the holder
    thread, and must not wait out the acquire budget to discover that. ``_SLOW_BUDGET``
    is the budget the holder would otherwise sit on; the unwind must beat it by a wide
    margin, which only an ABORTABLE acquire can do."""
    span_open = threading.Event()
    close_span = threading.Event()

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            span_open.set()
            close_span.wait(_ARRIVES)

    _spawn(joined, job)
    assert span_open.wait(_ARRIVES)

    async def rebuild() -> None:
        async with gate.exclusive_async(timeout=_SLOW_BUDGET):
            pass

    task = asyncio.create_task(rebuild())
    # Let the acquire park behind the open job span, then cancel it there.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=_STAYS_CLEAR)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    assert elapsed < _PROMPT, f"cancel during acquire took {elapsed:.1f}s — it waited out the budget"

    # No hold was ever taken, and nothing is left registered.
    assert gate._owner is None
    assert not gate.blocked
    close_span.set()
    # The gate is fully usable afterwards.
    async with asyncio.timeout(_ARRIVES), gate.exclusive_async(timeout=_ARRIVES):
        pass
    assert not gate.blocked


async def test_a_cancel_during_the_body_releases_promptly(gate: ForkGate):
    """The other cancel point: the hold IS taken and the body is running when the task
    is cancelled. The release rides the normal exit path, so it is prompt regardless of
    the acquire budget."""
    in_body = asyncio.Event()

    async def rebuild() -> None:
        async with gate.exclusive_async(timeout=_SLOW_BUDGET):
            in_body.set()
            await asyncio.Event().wait()  # park in the body until cancelled

    task = asyncio.create_task(rebuild())
    await asyncio.wait_for(in_body.wait(), timeout=_ARRIVES)
    assert gate.blocked

    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    assert elapsed < _PROMPT, f"cancel during the body took {elapsed:.1f}s"

    assert gate._owner is None
    assert not gate.blocked


async def test_exclusive_async_timeout_keeps_the_loud_proceed_semantics(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """The façade changes WHERE the hold lives, never the contract: an unquiesced job
    span logs at ERROR and the body still runs."""
    span_open = threading.Event()
    close_span = threading.Event()

    def job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            span_open.set()
            close_span.wait(_ARRIVES)

    _spawn(joined, job)
    assert span_open.wait(_ARRIVES)

    ran = False
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.exclusive_async(timeout=_TINY):
            ran = True
        assert "could not quiesce" in caplog.text
        assert "1 job span(s) still open" in caplog.text
    assert ran

    close_span.set()


def test_a_rebuild_blocked_by_another_rebuild_names_that_in_its_timeout(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """The timeout report distinguishes a foreign rebuild from an open job span —
    the two blockers need different operator responses."""
    held = threading.Event()
    release = threading.Event()

    def first() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            held.set()
            release.wait(_ARRIVES)

    _spawn(joined, first)
    assert held.wait(_ARRIVES)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        with gate.exclusive(timeout=_TINY):
            pass
        assert "another thread's rebuild holds the gate" in caplog.text
        assert "job span(s) still open" not in caplog.text

    release.set()


_needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")


def _fork_reporting(gate: ForkGate, report: Callable[[], bytes]) -> tuple[int, bytes]:
    """Fork; the child writes ``report()`` to a pipe and exits 0. Returns the child's exit code and report."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        code = 0
        try:
            os.close(read_end)
            os.write(write_end, report())
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as reader:
        payload = reader.read()
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), payload


@_needs_fork
def test_guard_forks_defers_a_fork_while_a_rebuild_is_held(gate: ForkGate, joined: list[threading.Thread]):
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    release = _hold_rebuild(gate, joined)
    forked = threading.Event()
    outcome: list[int] = []

    def fork() -> None:
        code, _ = _fork_reporting(gate, lambda: b"")
        outcome.append(code)
        forked.set()

    _spawn(joined, fork)
    assert not forked.wait(_STAYS_CLEAR)
    release.set()
    assert forked.wait(_ARRIVES)
    assert outcome == [0]


@_needs_fork
def test_guard_forks_releases_its_span_after_the_fork(gate: ForkGate):
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)

    code, _ = _fork_reporting(gate, lambda: b"")

    assert code == 0
    assert gate.live_spans == 0
    assert gate._fork_hooks.fork_spans == {}


@_needs_fork
def test_a_child_of_a_guarded_fork_starts_with_an_empty_gate(gate: ForkGate):
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)

    with gate.job_span(timeout=_ARRIVES):
        code, payload = _fork_reporting(gate, lambda: f"{gate.live_spans} {gate.blocked}".encode())
        assert gate.live_spans == 1

    assert code == 0
    assert payload == b"0 False"
    assert gate.live_spans == 0


@_needs_fork
def test_a_guarded_fork_reports_while_it_waits(gate: ForkGate, joined: list[threading.Thread]):
    ticks: list[float] = []
    release = _hold_rebuild(gate, joined)
    gate.guard_forks(timeout=_ARRIVES, tick=_release_on_tick(release, ticks, 3), tick_every=_TINY)

    code, _ = _fork_reporting(gate, lambda: b"")

    assert code == 0
    assert len(ticks) == 3


def _assert_forks_without_waiting(gate: ForkGate, caplog: pytest.LogCaptureFixture) -> None:
    """Fork under a guard whose wait would take ``_SLOW_BUDGET``; the fork must not wait at all."""
    started = time.monotonic()
    code, _ = _fork_reporting(gate, lambda: b"")
    assert time.monotonic() - started < _PROMPT
    assert code == 0
    assert "could not enter" not in caplog.text


@_needs_fork
def test_a_fork_made_by_the_rebuild_does_not_wait_on_that_rebuild(gate: ForkGate, caplog: pytest.LogCaptureFixture):
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"), gate.exclusive(timeout=_ARRIVES):
        _assert_forks_without_waiting(gate, caplog)
        assert gate.live_spans == 0
        assert gate.blocked

    assert gate._fork_hooks.fork_spans == {}


@_needs_fork
async def test_a_fork_from_a_rebuild_body_handed_to_the_loop_does_not_wait_on_that_rebuild(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """A rebuild held on a worker thread that runs its body on the loop (``run_coroutine_threadsafe``)."""
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()

    async def body() -> None:
        assert threading.get_ident() == loop_thread
        _assert_forks_without_waiting(gate, caplog)

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            asyncio.run_coroutine_threadsafe(body(), loop).result(_SLOW_BUDGET)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        await asyncio.to_thread(rebuild)


@_needs_fork
async def test_a_fork_from_an_async_rebuild_body_does_not_wait_on_that_rebuild(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.exclusive_async(timeout=_ARRIVES):
            _assert_forks_without_waiting(gate, caplog)
            await asyncio.to_thread(_assert_forks_without_waiting, gate, caplog)


@_needs_fork
async def test_a_fork_on_the_loop_outside_the_rebuild_still_waits_for_it(gate: ForkGate):
    """Another task on the loop a rebuild body runs on is not that rebuild."""
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    forked = asyncio.Event()
    release = asyncio.Event()

    async def rebuild() -> None:
        async with gate.exclusive_async(timeout=_ARRIVES):
            await release.wait()

    holding = asyncio.create_task(rebuild())
    assert await asyncio.to_thread(_poll_until, lambda: gate._owner is not None, _ARRIVES)

    async def fork() -> None:
        code, _ = await asyncio.to_thread(_fork_reporting, gate, lambda: b"")
        assert code == 0
        forked.set()

    forking = asyncio.create_task(fork())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(forked.wait(), _STAYS_CLEAR)
    release.set()
    await asyncio.wait_for(forking, _ARRIVES)
    await holding


@_needs_fork
async def test_a_fork_on_the_loop_thread_never_blocks_the_rebuild_body_running_on_that_loop(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """The rebuild body needs the loop the fork runs on: the fork goes ahead at once, loudly."""
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)
    body_resumed = asyncio.Event()

    async def rebuild() -> None:
        async with gate.exclusive_async(timeout=_ARRIVES):
            await asyncio.sleep(_TINY)
            body_resumed.set()

    async def fork() -> float:
        while gate._owner is None:
            await asyncio.sleep(_POLL)
        started = time.monotonic()
        code, _ = _fork_reporting(gate, lambda: b"")
        assert code == 0
        return time.monotonic() - started

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_guard"):
        holding = asyncio.create_task(rebuild())
        took = await asyncio.create_task(fork(), context=contextvars.Context())
        await asyncio.wait_for(body_resumed.wait(), _PROMPT)
        await holding

    assert took < _PROMPT
    assert "event loop's thread while a module re-import holds the gate" in caplog.text
    assert gate.live_spans == 0
    assert gate._fork_hooks.fork_spans == {}


@_needs_fork
async def test_a_fork_on_the_loop_thread_behind_a_pending_rebuild_enters_its_span_at_once(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """The fork instant counts as a job span the pending rebuild waits out, and nothing is reported."""
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)

    span_release = threading.Event()
    span_open = threading.Event()

    def hold_span() -> None:
        with gate.job_span(timeout=_ARRIVES):
            span_open.set()
            span_release.wait(_ARRIVES)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        _spawn(joined, hold_span)
        assert await asyncio.to_thread(span_open.wait, _ARRIVES)
        rebuilt = _pend_rebuild(gate, joined)

        started = time.monotonic()
        gate._fork_hooks.before()
        try:
            assert time.monotonic() - started < _PROMPT
            assert gate.live_spans == 2
        finally:
            gate._fork_hooks.after_in_parent()
        span_release.set()
        assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)

    assert caplog.text == ""
    assert gate.live_spans == 0
    assert gate._fork_hooks.fork_spans == {}


@_needs_fork
def test_a_fork_from_a_finished_rebuilds_context_waits_for_a_later_rebuild(
    gate: ForkGate, joined: list[threading.Thread]
):
    """Work the rebuild started inherits its context; once that rebuild ends, its forks are ordinary forks."""
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    with gate.exclusive(timeout=_ARRIVES):
        inherited = contextvars.copy_context()
    release = _hold_rebuild(gate, joined)
    forked = threading.Event()

    def fork() -> None:
        code, _ = inherited.run(_fork_reporting, gate, lambda: b"")
        assert code == 0
        forked.set()

    _spawn(joined, fork)
    assert not forked.wait(_STAYS_CLEAR)
    release.set()
    assert forked.wait(_ARRIVES)


_request_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_scope", default=None)


@_needs_fork
async def test_a_step_of_the_rebuild_run_in_a_fresh_context_forks_without_waiting(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)
    _request_scope.set("the driving request")

    async def step() -> None:
        assert _request_scope.get() is None
        _assert_forks_without_waiting(gate, caplog)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.exclusive_async(timeout=_ARRIVES):
            await asyncio.create_task(step(), context=gate.fresh_context())


@_needs_fork
def test_a_fresh_context_off_any_rebuild_forks_like_any_other(gate: ForkGate, joined: list[threading.Thread]):
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    fresh = gate.fresh_context()
    release = _hold_rebuild(gate, joined)
    forked = threading.Event()

    def fork() -> None:
        code, _ = fresh.run(_fork_reporting, gate, lambda: b"")
        assert code == 0
        forked.set()

    _spawn(joined, fork)
    assert not forked.wait(_STAYS_CLEAR)
    release.set()
    assert forked.wait(_ARRIVES)


@_needs_fork
def test_a_bare_fresh_context_under_a_held_rebuild_is_not_that_rebuild(gate: ForkGate):
    """Only :meth:`ForkGate.fresh_context` carries the rebuild across an emptied context."""
    gate.guard_forks(timeout=_TINY, tick=lambda: None)

    with gate.exclusive(timeout=_ARRIVES):
        started = time.monotonic()
        code, _ = contextvars.Context().run(_fork_reporting, gate, lambda: b"")

    assert code == 0
    assert time.monotonic() - started >= _TINY


def _pend_rebuild(gate: ForkGate, joined: list[threading.Thread]) -> threading.Event:
    """Start a rebuild on a helper thread that waits behind the open span; the returned event is set once it ran."""
    rebuilt = threading.Event()

    def rebuild() -> None:
        with gate.exclusive(timeout=_SLOW_BUDGET):
            rebuilt.set()

    _spawn(joined, rebuild)
    assert _poll_until(lambda: gate.blocked, _ARRIVES)
    return rebuilt


def test_a_job_span_nests_in_a_worker_thread_that_carries_the_jobs_context(
    gate: ForkGate, joined: list[threading.Thread]
):
    """A sync step a job hands to ``asyncio.to_thread`` runs inside the job's span: its own
    span enters at once behind a pending rebuild and registers nothing."""

    def step() -> float:
        started = time.monotonic()
        with gate.job_span(timeout=_SLOW_BUDGET):
            assert gate.live_spans == 1
        return time.monotonic() - started

    with gate.job_span(timeout=_ARRIVES):
        rebuilt = _pend_rebuild(gate, joined)
        assert asyncio.run(asyncio.to_thread(step)) < _PROMPT
        assert not rebuilt.is_set()
    assert rebuilt.wait(_ARRIVES)
    assert gate.live_spans == 0


def test_a_job_span_nests_in_a_fresh_context_taken_inside_the_job(gate: ForkGate, joined: list[threading.Thread]):
    """A step a job runs in a :meth:`ForkGate.fresh_context` on a thread that copies no
    context still runs inside the job's span: its own span enters at once behind a pending
    rebuild and registers nothing."""

    def step() -> float:
        started = time.monotonic()
        with gate.job_span(timeout=_SLOW_BUDGET):
            assert gate.live_spans == 1
        return time.monotonic() - started

    with gate.job_span(timeout=_ARRIVES):
        rebuilt = _pend_rebuild(gate, joined)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(gate.fresh_context().run, step).result() < _PROMPT
        assert not rebuilt.is_set()
    assert rebuilt.wait(_ARRIVES)
    assert gate.live_spans == 0


def test_a_fresh_context_taken_inside_a_closed_job_span_waits_like_any_other(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """The job-span mark a fresh context carries counts only while its span is open: once the
    span closed, a span entered in that context waits on a held rebuild like any other."""
    with gate.job_span(timeout=_ARRIVES):
        fresh = gate.fresh_context()
    release = _hold_rebuild(gate, joined)

    def step() -> None:
        with gate.job_span(timeout=_TINY):
            pass

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        fresh.run(step)
    release.set()
    assert "could not enter" in caplog.text


@_needs_fork
def test_a_fork_from_a_worker_thread_of_an_in_process_job_quiesces_cleanly(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """A job that runs a sync tool through ``asyncio.to_thread`` and forks there, with a
    rebuild pending, forks at once; the rebuild then runs after the job, with no timeout."""
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)

    async def job() -> None:
        await asyncio.to_thread(_assert_forks_without_waiting, gate, caplog)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        with gate.job_span(timeout=_ARRIVES):
            rebuilt = _pend_rebuild(gate, joined)
            asyncio.run(job())
            assert not rebuilt.is_set()
        assert rebuilt.wait(_ARRIVES)
    assert caplog.records == []
    assert gate.live_spans == 0
    assert gate._fork_hooks.fork_spans == {}


class _InterruptedRecord(dict[int, list[Any]]):
    """The fork hooks' per-thread span record, interrupted as the ``before`` hook records its span."""

    def setdefault(self, key: int, default: Any = None) -> Any:
        raise KeyboardInterrupt


def test_an_interrupt_while_the_fork_hook_records_its_span_leaves_no_span_open(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """CPython ignores an exception from a ``before`` fork hook and forks anyway; the span the
    hook was taking must not stay open, or every later rebuild would wait it out."""
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    gate._fork_hooks.fork_spans = _InterruptedRecord()

    # The exception is kept alive, as an unraisable hook may keep it: the span is closed by the
    # gate, not by the collection of the hook's frame.
    with pytest.raises(KeyboardInterrupt) as interrupted:
        gate._fork_hooks.before()
    gate._fork_hooks.after_in_parent()

    assert gate.live_spans == 0
    del interrupted
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"), gate.exclusive(timeout=_TINY):
        pass
    assert caplog.records == []


def test_a_second_guard_with_other_arguments_raises(gate: ForkGate):
    def tick() -> None:
        pass

    gate.guard_forks(timeout=_ARRIVES, tick=tick)

    with pytest.raises(RuntimeError, match="already guards forks"):
        gate.guard_forks(timeout=_ARRIVES + 1, tick=tick)


def test_a_second_guard_with_the_same_arguments_changes_nothing(gate: ForkGate):
    def tick() -> None:
        pass

    gate.guard_forks(timeout=_ARRIVES, tick=tick, tick_every=0.5)
    installed = gate._fork_hooks.guard

    gate.guard_forks(timeout=_ARRIVES, tick=tick, tick_every=0.5)

    assert gate._fork_hooks.guard is installed


@pytest.mark.parametrize("argument", ["timeout", "tick_every"])
def test_a_guard_with_non_positive_seconds_is_refused(gate: ForkGate, argument: str):
    arguments = {"timeout": _ARRIVES, "tick_every": 1.0, argument: 0.0}

    with pytest.raises(ValueError, match=f"{argument} must be positive seconds"):
        gate.guard_forks(tick=lambda: None, **arguments)
    assert gate._fork_hooks.guard is None


# -- the job span of a job that runs as a coroutine on an event loop ---------------------


async def _loop_turns_while(waiting: asyncio.Future[Any] | asyncio.Task[Any], budget: float) -> int:
    """Count the loop's turns while ``waiting`` is pending, for ``budget`` seconds at most."""
    turns = 0
    deadline = time.monotonic() + budget
    while not waiting.done() and time.monotonic() < deadline:
        await asyncio.sleep(_POLL)
        turns += 1
    return turns


async def test_an_async_job_span_waits_out_a_held_rebuild_without_holding_the_loop(
    gate: ForkGate, joined: list[threading.Thread]
):
    """A coroutine job that arrives while a rebuild holds the gate waits for it off the
    loop: the loop keeps turning, and the job's body runs only once the rebuild let go."""
    release = _hold_rebuild(gate, joined)
    ran: list[int] = []

    async def job() -> None:
        async with gate.job_span_async(timeout=_SLOW_BUDGET):
            ran.append(gate.live_spans)

    task = asyncio.create_task(job())
    assert await _loop_turns_while(task, _STAYS_CLEAR) > 1
    assert ran == []
    release.set()
    await asyncio.wait_for(task, _ARRIVES)
    assert ran == [1]
    assert gate.live_spans == 0


async def test_a_rebuild_waits_for_an_open_async_job_span(gate: ForkGate, joined: list[threading.Thread]):
    """A rebuild that starts while a coroutine job runs waits until the job's span closes,
    also while the job awaits a sync step on a ``to_thread`` worker."""
    rebuilt = threading.Event()
    step_may_end = threading.Event()

    def step() -> None:
        assert step_may_end.wait(_ARRIVES)

    async def job() -> None:
        async with gate.job_span_async(timeout=_ARRIVES):
            await asyncio.to_thread(step)

    task = asyncio.create_task(job())
    assert await _loop_turns_while(task, _STAYS_CLEAR) > 0
    assert gate.live_spans == 1

    def rebuild() -> None:
        with gate.exclusive(timeout=_SLOW_BUDGET):
            rebuilt.set()

    _spawn(joined, rebuild)
    assert await asyncio.to_thread(_poll_until, lambda: gate.blocked, _ARRIVES)
    assert not await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
    step_may_end.set()
    await asyncio.wait_for(task, _ARRIVES)
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)


async def test_an_async_job_span_cancelled_while_it_waits_leaves_no_span(
    gate: ForkGate, joined: list[threading.Thread]
):
    """A job cancelled while it waits for a held rebuild ends at once, registers no span,
    and its off-loop wait ends with it."""
    release = _hold_rebuild(gate, joined)
    waiters_before = threading.active_count()

    async def job() -> None:
        async with gate.job_span_async(timeout=_SLOW_BUDGET):
            raise AssertionError("the body of a cancelled wait must not run")

    task = asyncio.create_task(job())
    assert await _loop_turns_while(task, _STAYS_CLEAR) > 0
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < _PROMPT
    assert gate.live_spans == 0
    assert await asyncio.to_thread(_poll_until, lambda: threading.active_count() <= waiters_before, _ARRIVES)
    release.set()


async def test_an_async_job_span_past_its_budget_logs_and_enters(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """A wait that runs out logs at ERROR and enters anyway, registering the span."""
    release = _hold_rebuild(gate, joined)
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.job_span_async(timeout=_TINY):
            assert gate.live_spans == 1
    release.set()
    assert "a job span could not enter within 0.1s" in caplog.text
    assert gate.live_spans == 0


async def test_an_async_job_span_with_no_rebuild_enters_on_the_loop(gate: ForkGate):
    """With no rebuild pending the span enters at once, with no thread to wait on."""
    threads_before = threading.active_count()
    async with gate.job_span_async(timeout=_ARRIVES):
        assert gate.live_spans == 1
        assert threading.active_count() == threads_before
    assert gate.live_spans == 0


async def test_spans_inside_an_async_job_span_nest(gate: ForkGate, joined: list[threading.Thread]):
    """Inside a coroutine job's span, a nested async span and a sync span on a
    ``to_thread`` worker enter at once behind a pending rebuild and register nothing."""

    def sync_step() -> float:
        started = time.monotonic()
        with gate.job_span(timeout=_SLOW_BUDGET):
            assert gate.live_spans == 1
        return time.monotonic() - started

    async with gate.job_span_async(timeout=_ARRIVES):
        rebuilt = await asyncio.to_thread(_pend_rebuild, gate, joined)
        started = time.monotonic()
        async with gate.job_span_async(timeout=_SLOW_BUDGET):
            assert gate.live_spans == 1
        assert time.monotonic() - started < _PROMPT
        assert await asyncio.to_thread(sync_step) < _PROMPT
        assert not rebuilt.is_set()
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)
    assert gate.live_spans == 0


# --- a rebuild started inside a job ---------------------------------------------


def test_a_rebuild_started_inside_a_job_span_does_not_wait_on_that_span(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """A job that drives a rebuild itself holds the gate only against other work: the
    rebuild enters at once and reports nothing."""
    with gate.job_span(timeout=_ARRIVES):
        started = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"), gate.exclusive(timeout=_SLOW_BUDGET):
            assert gate.blocked
        assert time.monotonic() - started < _PROMPT
    assert not caplog.records


async def test_a_rebuild_a_coroutine_job_hands_to_a_worker_thread_does_not_wait_on_that_job(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """A coroutine job whose sync step runs the rebuild on a ``to_thread`` worker (the
    step carries the job's context) does not wait on its own job's span."""

    def rebuild() -> float:
        started = time.monotonic()
        with gate.exclusive(timeout=_SLOW_BUDGET):
            return time.monotonic() - started

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.job_span_async(timeout=_ARRIVES):
            assert await asyncio.to_thread(rebuild) < _PROMPT
    assert not caplog.records


async def test_an_async_rebuild_started_inside_an_async_job_span_does_not_wait_on_that_span(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """The async rebuild side passes its caller's own span the same way."""
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.job_span_async(timeout=_ARRIVES):
            started = time.monotonic()
            async with gate.exclusive_async(timeout=_SLOW_BUDGET):
                assert gate.blocked
            assert time.monotonic() - started < _PROMPT
    assert not caplog.records


def test_a_rebuild_started_inside_a_job_span_still_waits_for_other_jobs(gate: ForkGate, joined: list[threading.Thread]):
    """Only the caller's own span is passed: another job's open span still holds the
    rebuild until that job ends."""
    other_open = threading.Event()
    other_may_end = threading.Event()
    rebuilt = threading.Event()

    def other_job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            other_open.set()
            assert other_may_end.wait(_ARRIVES)

    def own_job() -> None:
        with gate.job_span(timeout=_ARRIVES), gate.exclusive(timeout=_SLOW_BUDGET):
            rebuilt.set()

    _spawn(joined, other_job)
    assert other_open.wait(_ARRIVES)
    _spawn(joined, own_job)
    assert not rebuilt.wait(_STAYS_CLEAR)
    other_may_end.set()
    assert rebuilt.wait(_ARRIVES)


# --- a job span parked while its holder waits on another party ---------------------


def _hold_rebuild_until_released(
    gate: ForkGate, joined: list[threading.Thread]
) -> tuple[threading.Event, threading.Event]:
    """Hold ``exclusive`` on a helper thread until the first returned event is set.

    The second event is set as the rebuild's body's last step, so a reader that sees it clear
    ran while the rebuild still held the gate.
    """
    release = threading.Event()
    leaving = threading.Event()
    held = threading.Event()

    def rebuild() -> None:
        with gate.exclusive(timeout=_ARRIVES):
            held.set()
            release.wait(_ARRIVES)
            leaving.set()

    _spawn(joined, rebuild)
    assert held.wait(_ARRIVES)
    return release, leaving


async def _locked() -> asyncio.Lock:
    """An ``asyncio.Lock`` already held, standing in for one another party holds."""
    lock = asyncio.Lock()
    await lock.acquire()
    return lock


async def _park_on(gate: ForkGate, lock: asyncio.Lock, parked: asyncio.Event | None = None) -> None:
    """Wait for ``lock`` with this context's span parked, then release it."""
    async with gate.parked_span(timeout=_ARRIVES):
        if parked is not None:
            parked.set()
        await lock.acquire()
    lock.release()


async def test_a_parked_span_does_not_hold_a_rebuild(gate: ForkGate, caplog: pytest.LogCaptureFixture):
    """A job waiting on a lock the rebuild's driver holds cannot import or fork: the rebuild
    enters at once, with no quiesce error."""
    lock = await _locked()
    parked = asyncio.Event()

    async def job() -> None:
        async with gate.job_span_async(timeout=_ARRIVES):
            await _park_on(gate, lock, parked)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        task = asyncio.create_task(job())
        await parked.wait()
        started = time.monotonic()
        async with gate.exclusive_async(timeout=_SLOW_BUDGET):
            assert time.monotonic() - started < _PROMPT
            assert gate.live_spans == 0
        lock.release()
        await asyncio.wait_for(task, _ARRIVES)
    assert not caplog.records
    assert gate.live_spans == 0


async def test_a_parked_span_counts_again_once_its_wait_is_over(gate: ForkGate, joined: list[threading.Thread]):
    """Once the wait is over the job runs again, so a later rebuild waits for its span."""
    lock = await _locked()
    parked = asyncio.Event()
    resumed = asyncio.Event()
    may_end = asyncio.Event()

    async def job() -> None:
        async with gate.job_span_async(timeout=_ARRIVES):
            await _park_on(gate, lock, parked)
            resumed.set()
            await may_end.wait()

    task = asyncio.create_task(job())
    await parked.wait()
    lock.release()
    await asyncio.wait_for(resumed.wait(), _ARRIVES)
    assert gate.live_spans == 1
    rebuilt = await asyncio.to_thread(_pend_rebuild, gate, joined)
    assert not await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
    may_end.set()
    await asyncio.wait_for(task, _ARRIVES)
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)


async def test_a_parked_span_waits_out_a_rebuild_that_holds_the_gate_when_its_wait_ends(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """The job's wait ends while another rebuild re-imports: the job's next line runs only once
    that rebuild has left."""
    lock = await _locked()
    parked = asyncio.Event()
    seen: list[bool] = []

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.job_span_async(timeout=_ARRIVES):
            task = asyncio.create_task(_park_on(gate, lock, parked))
            await parked.wait()
            release, leaving = await asyncio.to_thread(_hold_rebuild_until_released, gate, joined)
            lock.release()
            assert await _loop_turns_while(task, _STAYS_CLEAR) > 1
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, _ARRIVES)
            seen.append(leaving.is_set())
            assert gate.live_spans == 1
    assert seen == [True]
    assert not caplog.records


@pytest.mark.parametrize("stop", ["raise", "cancel", "timeout"])
async def test_a_park_left_early_counts_the_span_again_before_its_caller_runs_on(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture, stop: str
):
    """A wait that ends in an exception, a cancellation or its caller's timeout while another
    rebuild holds the gate: the code after the park (a ``finally``) runs only once that rebuild
    has left, and the exception, the cancellation or the timeout then propagates."""
    lock = await _locked()
    parked = asyncio.Event()
    go = asyncio.Event()
    after: list[bool] = []
    leaving = threading.Event()

    async def job() -> None:
        async with gate.job_span_async(timeout=_ARRIVES):
            try:
                if stop == "timeout":
                    async with asyncio.timeout(_STAYS_CLEAR):
                        await _park_on(gate, lock, parked)
                else:
                    async with gate.parked_span(timeout=_ARRIVES):
                        parked.set()
                        await go.wait()
                        raise ValueError("the wait failed")
            finally:
                after.append(leaving.is_set())
                assert gate.live_spans == 1

    expected = {"raise": ValueError, "cancel": asyncio.CancelledError, "timeout": TimeoutError}[stop]
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        task = asyncio.create_task(job())
        await parked.wait()
        release, leaving = await asyncio.to_thread(_hold_rebuild_until_released, gate, joined)
        if stop == "raise":
            go.set()
        elif stop == "cancel":
            task.cancel()
        assert await _loop_turns_while(task, 2 * _STAYS_CLEAR) > 1
        assert after == []
        release.set()
        with pytest.raises(expected):
            await asyncio.wait_for(task, _ARRIVES)
    assert after == [True]
    assert gate.live_spans == 0
    assert not caplog.records
    lock.release()


async def test_two_waits_of_one_job_park_its_span_once(gate: ForkGate, joined: list[threading.Thread]):
    """The first wait to end counts the span again and the second resumes at once; a rebuild
    started while one still waits and the other runs waits for the span."""
    first, second = await _locked(), await _locked()
    async with gate.job_span_async(timeout=_ARRIVES):
        parked = asyncio.Event()
        waits = [asyncio.create_task(_park_on(gate, first, parked)), asyncio.create_task(_park_on(gate, second))]
        await parked.wait()
        await _loop_turns_while(waits[1], _TINY)
        assert gate.live_spans == 0
        first.release()
        await asyncio.wait_for(waits[0], _ARRIVES)
        assert gate.live_spans == 1
        rebuilt = await asyncio.to_thread(_pend_rebuild, gate, joined)
        assert not await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
        second.release()
        started = time.monotonic()
        await asyncio.wait_for(waits[1], _ARRIVES)
        assert time.monotonic() - started < _PROMPT
        assert not rebuilt.is_set()
        assert gate.live_spans == 1
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)


async def test_a_job_that_ends_while_a_task_of_it_waits_parked_leaves_no_span(gate: ForkGate):
    """The job's span closes while a task carrying its mark still waits parked: the task's later
    re-admission registers nothing, so no rebuild waits for a span no job holds."""
    lock = await _locked()
    parked = asyncio.Event()
    async with gate.job_span_async(timeout=_ARRIVES):
        task = asyncio.create_task(_park_on(gate, lock, parked))
        await parked.wait()
    assert gate.live_spans == 0
    lock.release()
    await asyncio.wait_for(task, _ARRIVES)
    assert gate.live_spans == 0
    started = time.monotonic()
    async with gate.exclusive_async(timeout=_SLOW_BUDGET):
        assert time.monotonic() - started < _PROMPT


async def test_a_park_outside_any_job_span_parks_nothing(gate: ForkGate, joined: list[threading.Thread]):
    """Outside a span there is nothing to park: every open span still holds a rebuild."""
    running = threading.Event()
    may_end = threading.Event()

    def running_job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            running.set()
            assert may_end.wait(_ARRIVES)

    _spawn(joined, running_job)
    assert await asyncio.to_thread(running.wait, _ARRIVES)
    async with gate.parked_span(timeout=_ARRIVES):
        assert gate.live_spans == 1
        rebuilt = await asyncio.to_thread(_pend_rebuild, gate, joined)
        assert not await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
    may_end.set()
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)


async def test_a_park_in_a_held_rebuilds_body_parks_nothing(gate: ForkGate, caplog: pytest.LogCaptureFixture):
    """A held rebuild's body waits for nothing a rebuild quiesces for: the park leaves the span
    counted and returns at once instead of waiting on the rebuild's own pending count."""
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.job_span_async(timeout=_ARRIVES), gate.exclusive_async(timeout=_SLOW_BUDGET):
            started = time.monotonic()
            async with gate.parked_span(timeout=_SLOW_BUDGET):
                assert gate.live_spans == 1
            assert time.monotonic() - started < _PROMPT
    assert not caplog.records


async def test_a_parked_span_still_lets_a_running_span_hold_a_rebuild(gate: ForkGate, joined: list[threading.Thread]):
    """Only the parked span is passed: another job's span still holds the rebuild until that job ends."""
    lock = await _locked()
    parked = asyncio.Event()
    running = threading.Event()
    may_end = threading.Event()

    def running_job() -> None:
        with gate.job_span(timeout=_ARRIVES):
            running.set()
            assert may_end.wait(_ARRIVES)

    async def parked_job() -> None:
        async with gate.job_span_async(timeout=_ARRIVES):
            await _park_on(gate, lock, parked)

    _spawn(joined, running_job)
    assert await asyncio.to_thread(running.wait, _ARRIVES)
    task = asyncio.create_task(parked_job())
    await parked.wait()
    rebuilt = await asyncio.to_thread(_pend_rebuild, gate, joined)
    assert not await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
    may_end.set()
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)
    lock.release()
    await asyncio.wait_for(task, _ARRIVES)


async def test_a_re_admission_past_its_budget_logs_and_counts_the_span(
    gate: ForkGate, joined: list[threading.Thread], caplog: pytest.LogCaptureFixture
):
    """A rebuild holding the gate past the park's budget: the span-overrun ERROR is logged once
    and the span counts again, so the job is never wedged."""
    lock = await _locked()
    parked = asyncio.Event()

    async def wait() -> None:
        async with gate.parked_span(timeout=_TINY):
            parked.set()
            await lock.acquire()
        lock.release()

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with gate.job_span_async(timeout=_ARRIVES):
            task = asyncio.create_task(wait())
            await parked.wait()
            release = await asyncio.to_thread(_hold_rebuild, gate, joined)
            lock.release()
            await asyncio.wait_for(task, _ARRIVES)
            assert gate.live_spans == 1
            release.set()
    overruns = [record for record in caplog.records if "a job span could not enter within" in record.getMessage()]
    assert len(overruns) == 1


async def test_a_span_entered_beside_a_parked_wait_is_a_span_of_its_own(gate: ForkGate, joined: list[threading.Thread]):
    """While the job's span is parked, a span a sibling task or a ``to_thread`` step of the job
    enters does not nest in it: it waits behind a pending rebuild as any job's span would."""
    lock = await _locked()
    parked = asyncio.Event()

    def sync_step() -> int:
        with gate.job_span(timeout=_ARRIVES):
            return gate.live_spans

    async def sibling() -> int:
        async with gate.job_span_async(timeout=_ARRIVES):
            return gate.live_spans

    async with gate.job_span_async(timeout=_ARRIVES):
        waiting = asyncio.create_task(_park_on(gate, lock, parked))
        await parked.wait()
        release = await asyncio.to_thread(_hold_rebuild, gate, joined)
        spans = [asyncio.create_task(sibling()), asyncio.create_task(asyncio.to_thread(sync_step))]
        assert await _loop_turns_while(spans[0], _STAYS_CLEAR) > 1
        assert not any(span.done() for span in spans)
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*spans), _ARRIVES) == [1, 1]
        lock.release()
        await asyncio.wait_for(waiting, _ARRIVES)


async def test_a_coroutine_job_queued_behind_another_jobs_rebuild_does_not_hold_it(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """Two coroutine jobs each start a rebuild through one lock that serializes them, each
    waiting for it parked: the one queued on the lock does not hold the one running, and runs
    its own after it."""
    serial = asyncio.Lock()
    order: list[str] = []
    first_holds = asyncio.Event()

    async def job(name: str) -> None:
        async with gate.job_span_async(timeout=_ARRIVES):
            if name == "second":
                await first_holds.wait()
            async with gate.parked_span(timeout=_ARRIVES):
                await serial.acquire()
            try:
                if name == "first":
                    first_holds.set()
                    await asyncio.sleep(_STAYS_CLEAR)
                started = time.monotonic()
                async with gate.exclusive_async(timeout=_SLOW_BUDGET):
                    assert time.monotonic() - started < _PROMPT
                    order.append(name)
            finally:
                serial.release()

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        await asyncio.wait_for(asyncio.gather(job("first"), job("second")), _ARRIVES)
    assert order == ["first", "second"]
    assert not caplog.records


# --- the context of a task a rebuild's body starts ----------------------------------


@_needs_fork
def test_a_rebuild_context_belongs_to_the_rebuild_and_not_to_the_job_that_drove_it(
    gate: ForkGate, caplog: pytest.LogCaptureFixture
):
    """Taken inside a job span and a held rebuild: a fork in it passes while the rebuild holds
    the gate, and a span entered in it once the rebuild has left is a span of its own."""
    gate.guard_forks(timeout=_SLOW_BUDGET, tick=lambda: None)

    def spans_inside_a_span() -> int:
        with gate.job_span(timeout=_ARRIVES):
            return gate.live_spans

    with gate.job_span(timeout=_ARRIVES):
        with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"), gate.exclusive(timeout=_ARRIVES):
            context = gate.rebuild_context()
            context.run(_assert_forks_without_waiting, gate, caplog)
        assert context.run(spans_inside_a_span) == 2


@_needs_fork
def test_a_fork_in_a_rebuild_context_waits_for_a_later_rebuild(gate: ForkGate, joined: list[threading.Thread]):
    """The rebuild mark counts only while its rebuild holds the gate: afterwards a fork in the
    context waits on a later rebuild like any other."""
    gate.guard_forks(timeout=_ARRIVES, tick=lambda: None)
    with gate.job_span(timeout=_ARRIVES), gate.exclusive(timeout=_ARRIVES):
        context = gate.rebuild_context()
    release = _hold_rebuild(gate, joined)
    forked = threading.Event()

    def fork() -> None:
        code, _ = context.run(_fork_reporting, gate, lambda: b"")
        assert code == 0
        forked.set()

    _spawn(joined, fork)
    assert not forked.wait(_STAYS_CLEAR)
    release.set()
    assert forked.wait(_ARRIVES)


def test_a_rebuild_context_carries_none_of_its_callers_other_variables(gate: ForkGate):
    _request_scope.set("the driving request")
    assert gate.rebuild_context().run(_request_scope.get) is None
