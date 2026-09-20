"""The fork/rebuild barrier: mutual exclusion, writer preference, re-entrancy, the
async façade's single-thread ownership, and the bounded-and-loud timeout branches.

Waits are ``Event`` rendezvous wherever a rendezvous exists: a positive assertion
waits for the event with a generous budget, a negative assertion ("this must NOT
proceed") waits a short budget and asserts the event stayed clear. One condition has no
event to wait on — a gate flag another thread flips — and polls on a small sleep instead.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from tai42_kit.fork_gate import ForkGate, _resolve, fork_gate

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
def gate() -> ForkGate:
    """A private gate per test — the module singleton is process-wide state."""
    return ForkGate()


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

    close_span.set()


def test_reset_after_fork_drops_every_inherited_hold(gate: ForkGate):
    """A child forked from inside a job span inherits that span (and possibly a locked
    condition). The reset must leave a usable, empty gate — the child is a fresh
    single-threaded process, so nothing it inherited is its to release."""
    # The child's copy at the fork instant: its own span open, a rebuild registered by a
    # thread that does not exist post-fork, and the condition itself left locked. Set
    # directly — a real ``job_span`` here would decrement the freshly-reset counter below
    # zero on its way out.
    gate._live_spans = 1
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


async def test_resolve_settles_each_branch_exactly_once():
    """The acquire future's settler: success, failure, and the already-settled future a
    cancelled acquire leaves behind (settling it again would raise InvalidStateError)."""
    loop = asyncio.get_running_loop()

    ok: asyncio.Future[None] = loop.create_future()
    _resolve(ok, None)
    assert ok.result() is None

    boom = RuntimeError("acquire blew up")
    failed: asyncio.Future[None] = loop.create_future()
    _resolve(failed, boom)
    assert failed.exception() is boom

    cancelled: asyncio.Future[None] = loop.create_future()
    cancelled.cancel()
    # Must be a no-op, not an InvalidStateError.
    _resolve(cancelled, None)
    _resolve(cancelled, boom)
    assert cancelled.cancelled()


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
