"""The process-wide fork/rebuild barrier.

The invariant this gate exists to hold — the CANONICAL formulation; everywhere else
references it rather than restating it:

    **While the process re-imports its manifest modules, no child may be FORKED and
    no in-process job may RUN.**

A config reload pops every manifest module out of ``sys.modules`` and re-imports
it, holding that module's ``importlib._bootstrap._ModuleLock`` for the span of
its body. ``importlib`` registers no ``os.register_at_fork`` handler, so a child
forked during that span inherits the held per-module lock with an owner thread
that does not exist post-fork: the child blocks forever on the first import of
that module and nothing in the child can ever release it.

The two halves of the invariant have different shapes, because a fork COPIES the
module-lock table:

* a child forked OUTSIDE the window inherits unlocked copies and is immune to every
  later parent re-import, however long it lives — so for a forking pool the exposure
  is the ``fork()`` INSTANT, not the child's lifetime;
* an in-process job has no copy: it imports lazily against the same live
  ``sys.modules`` the reload is churning, so its exposure lasts as long as it runs.

Two sides, mutually exclusive:

* :meth:`ForkGate.job_span` is held by a work loop around whichever of those its pool
  needs — the fork instant, or the whole in-process job;
* :meth:`ForkGate.exclusive` is held by the rebuild for the span of the
  re-import (:meth:`ForkGate.exclusive_async` for a rebuild whose body is a
  coroutine on the serving loop).

Writer-preferring: a pending ``exclusive`` blocks new job spans, so a steady job
cadence cannot starve a reload.

Both waits are BOUNDED and LOUD. Neither side may wedge the other, so each
timeout logs at ERROR and PROCEEDS: a job is never dropped because a reload
overran, and a reload is never blocked forever by a runaway job. A timeout is the
barrier failing to hold — the outcome is exactly the unguarded behavior, now
reported instead of silent. Proceeding after a timeout is a DEGRADED mode, not a
weaker hold: see :class:`ForkGate` for what stops being guaranteed.

Import-safe: pure ``threading``, no dependencies. A process with no forking
backend never takes a job span, so ``exclusive`` costs one uncontended acquire.

A forking consumer must also call :meth:`ForkGate.reset_after_fork` from its
``os.register_at_fork(after_in_child=...)`` hook — a child inherits whatever holds
were live at the fork instant, and only the parent can ever release them.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

logger = logging.getLogger(__name__)


class ForkGate:
    """Mutual exclusion between forking job children and a module re-import.

    One instance per process (the module-level :data:`fork_gate` singleton). The
    two sides are held by different threads by construction — a work loop never
    takes :meth:`exclusive`, a rebuild never takes :meth:`job_span` — so the gate
    needs no lock ordering. ``exclusive`` is re-entrant per thread: a nested call
    on the owning thread passes straight through without self-deadlocking.
    (:meth:`exclusive_async` does NOT nest with another ``exclusive_async`` or with
    a thread-held ``exclusive``: each takes its own holder thread, so a second one
    is a different owner and waits. Nothing nests them today — the reload doors are
    serialized externally by the serving loop's ``reload_gate.lock``.)

    Degraded mode after a timeout
    -----------------------------
    A timed-out ``exclusive`` PROCEEDS and STEALS ownership from whoever holds it,
    because the alternative — blocking a reload forever behind a runaway job — is
    worse. While a stolen hold is outstanding, two guarantees weaken and one does
    not:

    * job-span exclusion still holds in the direction that matters — ``_pending_rebuilds``
      is incremented by every waiter and decremented only by its own release, so new
      job spans keep queueing until the LAST rebuild leaves;
    * rebuild-vs-rebuild exclusion is best-effort: two rebuild bodies can run at once
      (the stealer and the original holder);
    * re-entrancy is best-effort for the ORIGINAL holder: ``_owner`` now names the
      stealer, so the original's nested ``exclusive`` would re-wait rather than pass
      through.

    Both weakenings end when the stolen holder exits. The release is therefore
    CONDITIONAL (``if self._owner == tid``): an original holder finishing after being
    stolen from must not un-own the stealer, which would let a job span fork straight
    into the stealer's live re-import. Callers serialized by ``reload_gate.lock`` (every
    reload door in this codebase) never reach this state — only concurrent rebuild
    threads can.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        # Job spans currently open (forked children that may still be importing).
        self._live_spans = 0
        # Rebuilds pending OR holding. Non-zero is what makes the gate
        # writer-preferring: a new job span queues behind it.
        self._pending_rebuilds = 0
        # The thread holding ``exclusive``. Ownership alone decides re-entrancy: a
        # nested call sees its own tid and passes through, and only the OUTERMOST
        # call (the one that set the owner) clears it, so no depth count is needed.
        self._owner: int | None = None

    def reset_after_fork(self) -> None:
        """Drop every inherited hold — FORK-CHILD ONLY, never call in a live parent.

        The contract is part of this gate's public API because the only correct caller
        is a consumer's ``os.register_at_fork(after_in_child=...)`` hook, which lives in
        whichever package does the forking. Calling it anywhere else silently discards
        holds other threads still depend on.

        A child forked from inside a job span inherits ``_live_spans >= 1`` (its own
        span, which only the parent will ever close) and may inherit ``_cond`` itself in
        a LOCKED state, since the fork can land while another thread holds it. Left as
        is, the first gate call in the child either deadlocks on that lock or waits on a
        span nothing here will release. The child is a fresh single-threaded process, so
        the correct state is simply empty: a brand-new condition and zeroed counters.

        The parent's own gate is untouched — this mutates only the child's copy.
        """
        self._cond = threading.Condition()
        self._live_spans = 0
        self._pending_rebuilds = 0
        self._owner = None

    @property
    def blocked(self) -> bool:
        """Whether a rebuild is pending or in progress — i.e. whether a job span
        entered now would have to wait."""
        with self._cond:
            return self._pending_rebuilds > 0

    @property
    def live_spans(self) -> int:
        """How many job spans are currently open."""
        with self._cond:
            return self._live_spans

    @contextmanager
    def job_span(self, *, timeout: float) -> Iterator[None]:
        """Hold the work-loop side for as long as this pool's exposure lasts.

        WHAT the caller wraps is the caller's choice, and differs by pool (see the
        module docstring's invariant): a forking pool wraps the ``fork()`` INSTANT — a
        child forked outside a re-import window is immune thereafter — while a
        non-forking pool wraps the WHOLE in-process job, which imports against live
        ``sys.modules`` for its full duration. This method just holds whatever span it
        is given.

        Waits (bounded by ``timeout``) for any pending or in-progress rebuild to
        finish, then registers the span. A wait that exceeds the budget logs at
        ERROR and proceeds anyway — a queued job must never be lost to a reload
        that overran. The span is registered on BOTH paths, so a rebuild waiting behind
        it still waits out the span the caller declared.
        """
        # ``entered`` is set INSIDE the try, in the same block that increments the
        # count, so an async exception (``PyThreadState_SetAsyncExc``) landing between
        # the increment and the try cannot leak a span that nothing ever decrements.
        entered = False
        try:
            with self._cond:
                # The report is BUILT under the lock (it reads gate state) but EMITTED
                # outside it: logging can block on a handler, and under a green pool it
                # can yield, either of which would hold the gate's lock across an
                # unbounded operation.
                overran = not self._cond.wait_for(lambda: self._pending_rebuilds == 0, timeout=timeout)
                self._live_spans += 1
                entered = True
            if overran:
                logger.error(
                    "fork gate: a job span could not enter within %.1fs while a module re-import is in "
                    "progress; proceeding anyway — the child may deadlock on an inherited import lock",
                    timeout,
                )
            yield
        finally:
            if entered:
                with self._cond:
                    self._live_spans -= 1
                    self._cond.notify_all()

    def _blocker(self) -> str:
        """What is holding an ``exclusive`` wait open, for its timeout report. Called
        under ``_cond``. Both conditions can hold at once, so both are named."""
        reasons: list[str] = []
        if self._live_spans:
            reasons.append(f"{self._live_spans} job span(s) still open")
        if self._owner is not None:
            reasons.append(f"another thread's rebuild holds the gate (thread {self._owner})")
        return "; ".join(reasons) or "the wait raced its own condition"

    # Dispositions ``_enter_exclusive`` reports. ``REENTRANT`` owes no release; the
    # other two do (both registered a pending rebuild).
    _REENTRANT = "reentrant"
    _HELD = "held"
    _ABORTED = "aborted"

    def _enter_exclusive(self, tid: int, timeout: float, abort: threading.Event | None, out: list[str]) -> None:
        """Take the rebuilding side for ``tid``, recording the disposition in ``out``.

        ``out`` is an out-parameter rather than a return value on purpose: it is
        appended INSIDE the same locked block that mutates the counters (``list.append``
        is atomic), so an async exception landing between the mutation and the caller's
        assignment cannot leak a registration that nothing ever releases.

        ``abort`` (when given) is a second exit from the wait: the waiter abandons the
        acquire and reports ``_ABORTED`` — it never takes the hold. Setting the event
        alone is not enough to wake the wait; use :meth:`_signal_abort`.
        """
        blocker: str | None = None
        with self._cond:
            if self._owner == tid:
                out.append(self._REENTRANT)
                return
            self._pending_rebuilds += 1
            out.append(self._ABORTED)  # release owed from here on, whatever happens next

            def free() -> bool:
                return self._live_spans == 0 and self._owner is None

            quiesced = self._cond.wait_for(lambda: free() or (abort is not None and abort.is_set()), timeout=timeout)
            if abort is not None and abort.is_set():
                return
            if not quiesced:
                # Built here (it reads gate state), emitted below — see ``job_span``.
                blocker = self._blocker()
            self._owner = tid
            out[0] = self._HELD
        if blocker is not None:
            logger.error(
                "fork gate: a module re-import could not quiesce within %.1fs (%s); proceeding anyway — %s",
                timeout,
                blocker,
                "a live job child may deadlock on an inherited import lock",
            )

    def _leave_exclusive(self, tid: int) -> None:
        """Drop one registered rebuild for ``tid``."""
        with self._cond:
            # CONDITIONAL: a timed-out rebuild on another thread may have STOLEN
            # ownership while we ran. Clearing it then would un-own the stealer and let
            # a job span fork straight into its live re-import. The pending count is
            # ours to drop either way, and it is what keeps job spans queued until the
            # last rebuild leaves.
            if self._owner == tid:
                self._owner = None
            self._pending_rebuilds -= 1
            self._cond.notify_all()

    def _signal_abort(self, abort: threading.Event) -> None:
        """Set an acquire's abort event and wake the waiter.

        ``Condition.wait_for`` only re-evaluates its predicate on a notify (or at the
        timeout), so setting the event without notifying under ``_cond`` would leave an
        acquiring holder parked for its full budget.
        """
        with self._cond:
            abort.set()
            self._cond.notify_all()

    @contextmanager
    def exclusive(self, *, timeout: float) -> Iterator[None]:
        """Hold the rebuilding side for the span of a module re-import.

        Registers the intent first (so new job spans queue behind it), then waits
        (bounded by ``timeout``) for every open job span to close and for any
        other thread's rebuild to finish. A wait that exceeds the budget logs at
        ERROR — naming what actually blocked — and proceeds anyway: a runaway job
        must never block a reload forever. Re-entrant: a nested call on the owning
        thread passes straight through. A timed-out proceed STEALS ownership; see the
        class docstring for what that degrades.
        """
        tid = threading.get_ident()
        disposition: list[str] = []
        try:
            self._enter_exclusive(tid, timeout, None, disposition)
            yield
        finally:
            # Only a call that registered releases: a nested one never took the hold.
            if disposition and disposition[0] != self._REENTRANT:
                self._leave_exclusive(tid)

    @asynccontextmanager
    async def exclusive_async(self, *, timeout: float) -> AsyncIterator[None]:
        """:meth:`exclusive` for a rebuild whose body is a coroutine on the serving loop.

        The hold is taken and released on ONE dedicated thread that does nothing but
        hold it, so ownership stays with a single thread exactly as the re-entrancy
        model requires. Acquiring on a pooled ``to_thread`` worker and releasing on
        another would leave ``_owner`` pointing at a thread that no longer holds
        anything. The loop is never blocked: the acquire is awaited through a future
        the holder thread resolves, so the serving loop keeps running while the gate
        waits for job spans to drain.

        Timeout semantics are :meth:`exclusive`'s, unchanged — the holder thread logs
        at ERROR and proceeds, so the body always runs.

        Cancellable at BOTH points. ``release`` doubles as the holder's abort signal, so
        a cancellation landing mid-ACQUIRE unwinds promptly instead of waiting out the
        full budget (the holder abandons the acquire and takes no hold); a cancellation
        landing during the BODY releases on the way out like any other exit.
        """
        loop = asyncio.get_running_loop()
        held: asyncio.Future[None] = loop.create_future()
        # One event, two meanings: "the body is done, let go" and "abandon the acquire".
        # Both mean the same thing to the holder — stop holding and exit.
        release = threading.Event()
        done = threading.Event()

        def hold() -> None:
            tid = threading.get_ident()
            disposition: list[str] = []
            try:
                self._enter_exclusive(tid, timeout, release, disposition)
                if disposition and disposition[0] == self._ABORTED:
                    # The awaiting side gave up before the hold was taken. Its future is
                    # already settled (cancelled), so this resolve is a defensive no-op
                    # that also guarantees no caller can await a future nobody settles.
                    loop.call_soon_threadsafe(_resolve, held, _AcquireAbandoned())
                    return
                loop.call_soon_threadsafe(_resolve, held, None)
                release.wait()
            except BaseException as exc:  # pragma: no cover - the acquire path does not raise
                loop.call_soon_threadsafe(_resolve, held, exc)
            finally:
                if disposition and disposition[0] != self._REENTRANT:
                    self._leave_exclusive(tid)
                done.set()

        holder = threading.Thread(target=hold, name="tai-fork-gate-hold", daemon=True)
        holder.start()
        try:
            await held
        except BaseException:
            # A cancellation (or a failed acquire) here would otherwise strand the holder
            # thread holding the gate for the process lifetime, blocking every later
            # rebuild and job span. Abort it — signalled under ``_cond`` so a holder still
            # parked in its acquire wakes NOW rather than at the budget — then unwind.
            await self._unwind(release, done)
            raise
        try:
            yield
        finally:
            await self._unwind(release, done)

    async def _unwind(self, release: threading.Event, done: threading.Event) -> None:
        """Signal the holder thread to let go and wait for it off-loop.

        The signal goes out FIRST and under ``_cond``, so the release is always ordered
        before this returns even if the join below is itself cancelled. The join is what
        makes the release OBSERVED before return — a caller that starts a job span
        immediately after must not race it. On a cancelled join that observation is lost
        (the holder still exits promptly, it is simply no longer awaited); nothing in this
        codebase starts a job span on the cancellation path of a reload.
        """
        self._signal_abort(release)
        await asyncio.to_thread(done.wait)


class _AcquireAbandoned(RuntimeError):
    """The async acquire was abandoned before the hold was taken (its awaiter went away)."""


def _resolve(future: asyncio.Future[None], exc: BaseException | None) -> None:
    """Settle the acquire future from the holder thread, ignoring a future the loop
    already cancelled (the awaiting task went away while the gate was being taken)."""
    if future.done():
        return
    if exc is None:
        future.set_result(None)
    else:
        future.set_exception(exc)


# The one process-wide gate. A forking work loop takes ``job_span``; the reload
# gate takes ``exclusive`` around every heavy reload body, and a serving-loop
# rebuild takes ``exclusive_async``.
fork_gate = ForkGate()
