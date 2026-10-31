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
  needs — the fork instant, or the whole in-process job (:meth:`ForkGate.job_span_async`
  for a job that runs as a coroutine on an event loop);
* :meth:`ForkGate.exclusive` is held by the rebuild for the span of the
  re-import (:meth:`ForkGate.exclusive_async` for a rebuild whose body is a
  coroutine on the serving loop).

Writer-preferring: a pending ``exclusive`` blocks new job spans, so a steady job
cadence cannot starve a reload.

A rebuild waits out every open job span. A span whose holder is waiting on a lock another
party holds is parked for that wait (:meth:`ForkGate.parked_span`): it cannot import or fork
while it waits, and it counts again before its holder runs. The span open in the context that
starts the rebuild is that rebuild's own and never holds it.

Both waits are BOUNDED and LOUD. Neither side may wedge the other, so each
timeout logs at ERROR and PROCEEDS: a job is never dropped because a reload
overran, and a reload is never blocked forever by a runaway job. A timeout is the
barrier failing to hold — the outcome is exactly the unguarded behavior, now
reported instead of silent. Proceeding after a timeout is a DEGRADED mode, not a
weaker hold: see :class:`ForkGate` for what stops being guaranteed.

A job span waiting on a rebuild can report progress: given a ``tick``, it calls it once
per ``tick_every`` seconds of waiting, so a work loop whose next pass is held up by a
re-import keeps telling its liveness probe it is alive.

Import-safe: ``threading`` and ``asyncio`` only, no dependencies. A process that runs
no backend jobs (no fork and no in-process job takes a span) pays one uncontended acquire
for ``exclusive``.

A forking consumer calls :meth:`ForkGate.guard_forks` once from its build: the guard
(:mod:`tai42_kit.fork_guard`) holds a job span around EVERY fork of the process, whichever
library forks, except a fork the rebuild's own body makes, and resets the child's copy of
the gate (a child inherits whatever holds were live at the fork instant, and only the parent
can ever release them).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager

from tai42_kit.fork_guard import ForkGuard, ForkHooks
from tai42_kit.thread_wait import hold_on_thread, wait_on_thread

logger = logging.getLogger(__name__)

JOB_SPAN_WAIT_SECONDS = 120.0
"""How long a backend's job span waits for a re-import before it logs at ERROR and proceeds.

Sized generously against a rebuild, so a job already dequeued waits out a re-import and is
never dropped: past it the span enters anyway and the job runs.
"""


def _require_positive_seconds(name: str, value: float) -> None:
    """Refuse a duration that is not a positive, finite number of seconds."""
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be positive seconds, got {value!r}")


_rebuild_hold: contextvars.ContextVar[object | None] = contextvars.ContextVar("fork_gate_rebuild_hold", default=None)
"""The hold of the rebuild whose body runs in this context, or ``None``.

Set in the context that runs a rebuild's body once the hold is taken. A rebuild hands its
body across threads with ``asyncio`` primitives that carry the caller's context (a
``to_thread`` worker, a coroutine marshalled onto a loop with ``run_coroutine_threadsafe``,
a task created from the body), so the mark follows the body to whichever thread runs it.
A step the body runs in a context of its own starts from one of two contexts that carry
this mark: :meth:`ForkGate.rebuild_context` for a step whose work outlives the call that
starts it (a task that runs on after the rebuild, a deferred tail), which carries this mark
alone, and :meth:`ForkGate.fresh_context` for a step the caller waits for, which carries
the job-span mark too.
"""

_job_span_mark: contextvars.ContextVar[object | None] = contextvars.ContextVar("fork_gate_job_span", default=None)
"""The outermost job span open in this context, or ``None``.

Set in the context a job span's body runs in, so it follows the job wherever ``asyncio``
carries that context: a sync step handed to a ``to_thread`` worker, a task the job creates,
an executor run through ``contextvars.copy_context``, a step started from
:meth:`ForkGate.fresh_context`. A span or a fork entered in a context marked with a span the
gate still holds nests in it; while that span is parked (:meth:`ForkGate.parked_span`) it is
a span of its own.
"""


class ForkGate:
    """Mutual exclusion between a module re-import and the work that must not overlap it: a fork, or an in-process job.

    One instance per process (the module-level :data:`fork_gate` singleton). A work
    loop never takes :meth:`exclusive`. A rebuild never waits on a job span of its own:
    the span open in the context that starts it (a job that drives a rebuild itself) is
    no blocker, its body enters none, and a fork the body makes — on whichever thread runs it, and
    in any step it runs in a :meth:`fresh_context` — is the rebuild's own fork, which the
    fork guard lets through without a span and without protection (see
    :meth:`guard_forks`). So the gate needs no lock ordering. Both sides are re-entrant,
    so a nested call passes straight through without self-deadlocking. A job span nests
    in the span open in the caller's context (:data:`_job_span_mark`), on whichever thread
    the job's context runs: a span on a thread that does not carry that context (a thread
    started without it, a greenlet, which starts from an empty context) is a span of its
    own and waits. ``exclusive`` nests per thread: a thread is ``threading.get_ident()`` at
    call time, so under a gevent-patched process each greenlet is its own thread here.
    (:meth:`exclusive_async` does NOT nest with another ``exclusive_async`` or with
    a thread-held ``exclusive``: each takes its own holder thread, so a second one
    is a different owner and waits. Nothing nests them: the reload doors are
    serialized externally by the serving loop's reload gate.)

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
    into the stealer's live re-import. Callers serialized by the reload gate (every
    reload door in this codebase) never reach this state — only concurrent rebuild
    threads can.
    """

    def __init__(self) -> None:
        """Create an empty gate: no live spans, no pending rebuilds, no owner."""
        self._cond = threading.Condition()
        # Rebuilds pending OR holding. Non-zero is what makes the gate
        # writer-preferring: a new job span queues behind it.
        self._pending_rebuilds = 0
        # The thread holding ``exclusive``. Ownership alone decides re-entrancy: a
        # nested call sees its own tid and passes through, and only the OUTERMOST
        # call (the one that set the owner) clears it, so no depth count is needed.
        self._owner: int | None = None
        # The job spans open now, each the mark of an outermost span (a fork's, or a
        # job's). A span entered in a context marked with one of them
        # (``_job_span_mark``) nests and registers nothing.
        self._open_spans: set[object] = set()
        # The holds of the rebuilds that hold the gate now: a context marked with one of
        # them (``_rebuild_hold``) runs that rebuild's body.
        self._held_rebuilds: set[object] = set()
        # The job spans parked while their holders wait on another party (``parked_span``):
        # out of ``_open_spans``, so no rebuild waits for them, until their wait is over.
        self._parked: set[object] = set()
        # Bumped by ``reset_after_fork``: a span entered before the reset belongs to the
        # parent's gate, so leaving it in the child must not touch the child's counters.
        self._generation = 0
        # The fork hooks ``guard_forks`` installs, and what they hold for the forks in progress.
        self._fork_hooks = ForkHooks(self)

    def reset_after_fork(self) -> None:
        """Drop every inherited hold — FORK-CHILD ONLY, never call in a live parent.

        The caller is the ``after_in_child`` hook :meth:`guard_forks` registers. It stays
        public so a test can model a child's copy of the gate; calling it in a live
        process silently discards holds other threads still depend on.

        A child forked from inside a job span inherits that span open (its own span,
        which only the parent will ever close) and may inherit ``_cond`` itself in
        a LOCKED state, since the fork can land while another thread holds it. Left as
        is, the first gate call in the child either deadlocks on that lock or waits on a
        span nothing here will release. The child is a fresh single-threaded process, so
        the correct state is simply empty: a brand-new condition, zeroed counters, no open
        span on any thread. A span the child inherited open and later leaves changes
        nothing in the reset gate. The fork guard stays installed: the child's own forks
        are guarded too.

        The parent's own gate is untouched — this mutates only the child's copy.
        """
        self._cond = threading.Condition()
        self._generation += 1
        self._pending_rebuilds = 0
        self._owner = None
        self._held_rebuilds = set()
        self._open_spans = set()
        self._parked = set()
        self._fork_hooks.forget_forks()

    def fresh_context(self) -> contextvars.Context:
        """An empty :class:`contextvars.Context` that still belongs to the rebuild and the job span running here.

        For a step that must not inherit its caller's context and that its caller waits for
        (a runner on a thread that copies no context, run to its end): run it in this context
        instead of a bare ``contextvars.Context()``. Every variable reads its default except
        the fork gate's own two marks: the mark of the rebuild whose body runs in the calling
        context, so a fork the step makes is still that rebuild's own fork, and the mark of
        the job span open in the calling context, so a span or a fork the step makes nests in
        that span (see :meth:`guard_forks`). A mark counts only while its rebuild or span is
        still open. Called outside both, it is a bare empty context. A context may be entered
        by one task or thread at a time, so each step takes its own.

        A step whose work outlives the call that starts it takes :meth:`rebuild_context`
        instead: that work is not the job's.
        """
        return self._context_marked_by(_rebuild_hold, _job_span_mark)

    def rebuild_context(self) -> contextvars.Context:
        """An empty :class:`contextvars.Context` that still belongs to the rebuild running here, and to no job span.

        For a step of a rebuild's body whose work outlives the call that starts it (a task
        that runs on after the rebuild, a deferred tail of it): run it in this context. Every
        variable reads its default except the mark of the rebuild whose body runs in the
        calling context, so a fork the step makes while that rebuild holds the gate is the
        rebuild's own fork. It carries no job-span mark: the work outlives the job that drove
        the rebuild, so a span or a fork it makes once the rebuild has left is a span of its
        own, and a wait it parks parks nothing of that job's. Called outside a rebuild's body,
        it is a bare empty context. A context may be entered by one task or thread at a time,
        so each step takes its own.

        A step its caller waits for takes :meth:`fresh_context` instead.
        """
        return self._context_marked_by(_rebuild_hold)

    @staticmethod
    def _context_marked_by(*variables: contextvars.ContextVar[object | None]) -> contextvars.Context:
        """An empty context carrying the calling context's value of each of ``variables`` that is set."""
        context = contextvars.Context()
        for variable in variables:
            mark = variable.get()
            if mark is not None:
                context.run(variable.set, mark)
        return context

    @property
    def blocked(self) -> bool:
        """Whether a rebuild is pending or in progress (a job span entered now would have to wait)."""
        with self._cond:
            return self._pending_rebuilds > 0

    @property
    def live_spans(self) -> int:
        """How many job spans are currently open."""
        with self._cond:
            return len(self._open_spans)

    @contextmanager
    def job_span(
        self, *, timeout: float, tick: Callable[[], None] | None = None, tick_every: float = 1.0
    ) -> Iterator[None]:
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

        ``tick`` is the waiting loop's progress report: while the span waits it is called
        once per ``tick_every`` seconds, outside the gate's lock; an entry that does not
        wait never calls it. A tick that raises is logged at ERROR once and that wait
        ticks no more, so a tick that refuses to report keeps refusing. ``tick_every``
        must be positive seconds (``ValueError`` otherwise).

        Re-entrant per context: a span entered in a context that carries an open span of
        this gate enters at once and registers nothing, because the outer span already
        covers the window. A fork from inside an in-process job is the case, on the job's
        own thread or on a worker the job hands a sync step to with ``asyncio.to_thread``.
        """
        _require_positive_seconds("tick_every", tick_every)
        with self._cond:
            nested = _job_span_mark.get() in self._open_spans
        if nested:
            yield
            return
        span = object()
        # ``entered`` receives the gate generation INSIDE the locked block that registers
        # the span, so an async exception (``PyThreadState_SetAsyncExc``) landing between
        # the registration and the try cannot leak a span that nothing ever releases.
        entered: list[int] = []
        try:
            self._enter_reported_span(span, timeout, tick, tick_every, entered.append)
            mark = _job_span_mark.set(span)
            try:
                yield
            finally:
                _job_span_mark.reset(mark)
        finally:
            if entered:
                self._leave_span(span, entered[0])

    @asynccontextmanager
    async def job_span_async(self, *, timeout: float) -> AsyncIterator[None]:
        """:meth:`job_span` for a job that runs as a coroutine on an event loop.

        The wait, timeout and nesting are :meth:`job_span`'s. A job span entered on the
        loop thread must never block it: the rebuild it waits for, and the other jobs
        whose spans that rebuild waits out, run on the same loop. So with no rebuild
        pending the span registers at once on the loop, and while one is, the wait runs
        on a thread of its own that does nothing else, awaited through a future the loop
        keeps turning around. The span is always registered on the loop, so a job
        cancelled while it waits ends the wait and registers nothing. No ``tick``: the
        loop's own passes go on while the job waits.
        """
        with self._cond:
            nested = _job_span_mark.get() in self._open_spans
        if nested:
            yield
            return
        span = object()
        # Filled inside the locked block that registers the span; see ``job_span``.
        entered: list[int] = []
        try:
            if await self._enter_span_async(span, timeout, entered.append):
                _report_span_overrun(timeout)
            mark = _job_span_mark.set(span)
            try:
                yield
            finally:
                _job_span_mark.reset(mark)
        finally:
            if entered:
                self._leave_span(span, entered[0])

    async def _enter_span_async(self, span: object, timeout: float, record: Callable[[int], None]) -> bool:
        """Register the job span ``span`` from the loop; return whether its wait overran.

        Registers under the lock once no rebuild is pending, or at the deadline. Until
        then each pass awaits :meth:`_admission_wait`, and looks again when it returns:
        a rebuild that became pending meanwhile keeps the span waiting, as the gate
        prefers rebuilds.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                admitted = self._pending_rebuilds == 0
                if admitted or time.monotonic() >= deadline:
                    record(self._generation)
                    self._open_spans.add(span)
                    return not admitted
            await self._admission_wait(deadline)

    async def _admission_wait(self, deadline: float) -> None:
        """Wait off the loop until no rebuild is pending, or until ``deadline``.

        The wait runs on a dedicated thread (:func:`~tai42_kit.thread_wait.wait_on_thread`),
        not the loop's executor: a rebuild's body may hand its own steps to that executor,
        which waiting jobs must not fill. A cancellation of the awaiting task wakes the
        thread at once.
        """

        def wait(stop: threading.Event) -> None:
            with self._cond:
                self._cond.wait_for(
                    lambda: self._pending_rebuilds == 0 or stop.is_set(),
                    timeout=max(deadline - time.monotonic(), 0.0),
                )

        await wait_on_thread(wait, wake=self._wake, name="tai-fork-gate-span-wait")

    @asynccontextmanager
    async def parked_span(self, *, timeout: float) -> AsyncIterator[None]:
        """Park the job span open in this context while its holder waits on another party.

        For a wait inside a job span on something another party holds, a lock above all,
        whose holder may itself be starting a rebuild: the waiting holder cannot import or
        fork, so for the body of this block no rebuild waits for its span. On the way out,
        whichever way the body ends (normally, with an exception, cancelled), the span
        counts again before the caller's next line runs: if a rebuild is pending or holds
        the gate then, the exit waits it out as a job span's entry does, bounded by
        ``timeout`` seconds from the exit and LOUD past it (the span-overrun ERROR, then
        the span counts anyway). A cancellation that arrives during that wait is held until
        the span counts again and raised then. ``timeout`` must be positive seconds
        (``ValueError`` otherwise).

        The span is parked once for all the waits of one job: the first wait to end counts
        it again, and a later one ends at once. A span entered or a fork made while the
        span is parked, in this context or in one that carries it, does not nest in it: it
        is a span of its own and waits behind a pending rebuild. The gate tracks spans, not
        threads: a task that shares the job's context and imports, entering no span, while the
        span is parked runs unguarded. A job whose span closes while one of its waits is
        parked leaves nothing behind.

        Parks nothing outside a job span, and nothing in a held rebuild's body, which a
        rebuild never waits for.
        """
        _require_positive_seconds("timeout", timeout)
        span = _job_span_mark.get()
        with self._cond:
            parked = _rebuild_hold.get() not in self._held_rebuilds and (
                span in self._open_spans or span in self._parked
            )
            if parked and span in self._open_spans:
                self._open_spans.remove(span)
                self._parked.add(span)
                # A rebuild waiting for this span may now be free to start.
                self._cond.notify_all()
        try:
            yield
        finally:
            if parked:
                await self._readmit(span, timeout)

    async def _readmit(self, span: object, timeout: float) -> None:
        """Count the parked ``span`` again once no rebuild is pending, or at the deadline.

        The deadline is ``timeout`` seconds from now. A span no longer parked (another wait
        of its job counted it again, or the job closed it) has nothing to do. A cancellation
        of the awaiting task is held until the span counts again, then raised: the caller's
        code after the park must not run while a rebuild holds the gate.
        """
        deadline = time.monotonic() + timeout
        cancelled: asyncio.CancelledError | None = None
        overran = False
        while True:
            with self._cond:
                if span not in self._parked:
                    break
                admitted = self._pending_rebuilds == 0
                if admitted or time.monotonic() >= deadline:
                    self._parked.remove(span)
                    self._open_spans.add(span)
                    overran = not admitted
                    break
            try:
                await self._admission_wait(deadline)
            except asyncio.CancelledError as exc:
                cancelled = exc
        if overran:
            _report_span_overrun(timeout)
        if cancelled is not None:
            raise cancelled

    def _enter_span(
        self,
        span: object,
        timeout: float,
        tick: Callable[[], None] | None,
        tick_every: float,
        record: Callable[[int], None],
    ) -> bool:
        """Register the job span ``span``, handing the gate generation to ``record``; return whether it overran.

        ``record`` runs under the gate's lock just before the span is registered, so a
        caller that is interrupted after the registration has always recorded what to close.

        The wait runs in slices of ``tick_every`` when there is a ``tick``, calling it
        between slices. The report is BUILT under the lock but every tick and log is
        EMITTED outside it: either can block on I/O, and under a green pool it can yield,
        which would hold the gate's lock across an unbounded operation.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                remaining = deadline - time.monotonic()
                wait = remaining if tick is None else min(tick_every, remaining)
                admitted = self._cond.wait_for(lambda: self._pending_rebuilds == 0, timeout=max(wait, 0.0))
                if admitted or time.monotonic() >= deadline:
                    record(self._generation)
                    self._open_spans.add(span)
                    return not admitted
            if tick is not None:
                try:
                    tick()
                except Exception:
                    logger.exception(
                        "fork gate: the progress tick during a job span's wait failed; "
                        "this wait reports no more progress"
                    )
                    tick = None

    def _leave_span(self, span: object, generation: int) -> None:
        """Close the job span ``span``, recorded under ``generation``.

        A span recorded but never registered (its caller was interrupted between the two)
        has nothing to close; a parked one is dropped from the parked spans.
        """
        with self._cond:
            if generation != self._generation:
                # Entered in the parent before this child's reset.
                return
            # A span parked now leaves with its job: its pending re-admission finds nothing.
            self._parked.discard(span)
            if span not in self._open_spans:
                # Parked, or never registered.
                return
            self._open_spans.remove(span)
            self._cond.notify_all()

    def _enter_span_at_once(self, span: object, record: Callable[[int], None]) -> bool:
        """Register the job span ``span`` without waiting; return whether a rebuild holds the gate now.

        For a span whose holder must not wait, as :meth:`_enter_span` does, for a pending
        rebuild. A rebuild still waiting for job spans to close waits out this one too; one that already
        holds the gate is past that wait, so the span runs inside its re-import.
        ``record`` runs under the lock as in :meth:`_enter_span`.
        """
        with self._cond:
            record(self._generation)
            self._open_spans.add(span)
            return bool(self._held_rebuilds)

    def _enter_reported_span(
        self,
        span: object,
        timeout: float,
        tick: Callable[[], None] | None,
        tick_every: float,
        record: Callable[[int], None],
    ) -> None:
        """:meth:`_enter_span`, reporting at ERROR a span that entered only because its wait ran out."""
        if self._enter_span(span, timeout, tick, tick_every, record):
            _report_span_overrun(timeout)

    def _runs_own_work(self) -> bool:
        """Whether this context runs a held rebuild's body or an open job span. Called under ``_cond``."""
        return _rebuild_hold.get() in self._held_rebuilds or _job_span_mark.get() in self._open_spans

    def guard_forks(self, *, timeout: float, tick: Callable[[], None], tick_every: float = 1.0) -> None:
        """Hold a job span around every fork of this process but a rebuild's own (see :mod:`tai42_kit.fork_guard`).

        Each fork waits as :meth:`job_span` waits with ``timeout``, ``tick`` and
        ``tick_every``, but a fork on a thread that runs an event loop, which never waits.
        One guard per gate: a second call with the same arguments changes
        nothing, one with other arguments raises ``RuntimeError`` (a process has one work loop
        to report for). ``timeout`` and ``tick_every`` must be positive seconds
        (``ValueError`` otherwise); they are checked here because a fork hook cannot raise to
        its caller.
        """
        _require_positive_seconds("timeout", timeout)
        _require_positive_seconds("tick_every", tick_every)
        self._fork_hooks.install(ForkGuard(timeout=timeout, tick=tick, tick_every=tick_every))

    def _other_spans(self, own_span: object | None) -> set[object]:
        """The open job spans a rebuild started in ``own_span`` waits for. Called under ``_cond``.

        Every open span but that one; a parked span is not open (see :meth:`parked_span`).
        """
        return self._open_spans - {own_span}

    def _blocker(self, own_span: object | None) -> str:
        """What is holding an ``exclusive`` wait open, for its timeout report.

        Called under ``_cond``. Both conditions can hold at once, so both are named.
        """
        reasons: list[str] = []
        others = self._other_spans(own_span)
        if others:
            reasons.append(f"{len(others)} job span(s) still open")
        if self._owner is not None:
            reasons.append(f"another thread's rebuild holds the gate (thread {self._owner})")
        return "; ".join(reasons) or "the wait raced its own condition"

    # Dispositions ``_enter_exclusive`` reports. ``REENTRANT`` owes no release; the
    # other two do (both registered a pending rebuild).
    _REENTRANT = "reentrant"
    _HELD = "held"
    _ABORTED = "aborted"

    def _enter_exclusive(
        self,
        tid: int,
        hold: object,
        own_span: object | None,
        timeout: float,
        abort: threading.Event | None,
        out: list[str],
    ) -> None:
        """Take the rebuilding side for ``tid`` as ``hold``, recording the disposition in ``out``.

        ``own_span`` is the job-span mark of the context that starts the rebuild: that span
        is the caller's own and never holds the wait (the others still do).

        ``hold`` names this rebuild among the held ones from the moment it holds the gate
        until :meth:`_leave_exclusive` drops it; a reentrant entry registers nothing.

        ``out`` is an out-parameter rather than a return value on purpose: it is
        appended INSIDE the same locked block that mutates the counters (``list.append``
        is atomic), so an async exception landing between the mutation and the caller's
        assignment cannot leak a registration that nothing ever releases.

        ``abort`` (when given) is a second exit from the wait: the waiter abandons the
        acquire and reports ``_ABORTED`` — it never takes the hold. Setting the event
        alone is not enough to wake the wait: :meth:`_wake` it after setting it.
        """
        blocker: str | None = None
        with self._cond:
            if self._owner == tid:
                out.append(self._REENTRANT)
                return
            self._pending_rebuilds += 1
            out.append(self._ABORTED)  # release owed from here on, whatever happens next

            def free() -> bool:
                return not self._other_spans(own_span) and self._owner is None

            quiesced = self._cond.wait_for(lambda: free() or (abort is not None and abort.is_set()), timeout=timeout)
            if abort is not None and abort.is_set():
                return
            if not quiesced:
                # Built here (it reads gate state), emitted below — see ``job_span``.
                blocker = self._blocker(own_span)
            self._owner = tid
            self._held_rebuilds.add(hold)
            out[0] = self._HELD
        if blocker is not None:
            logger.error(
                "fork gate: a module re-import could not quiesce within %.1fs (%s); proceeding anyway — %s",
                timeout,
                blocker,
                "a job still running during the re-import may import against a half-torn module table, "
                "and a forked child may deadlock on an inherited import lock",
            )

    def _leave_exclusive(self, tid: int, hold: object) -> None:
        """Drop one registered rebuild for ``tid``, held (or abandoned) as ``hold``."""
        with self._cond:
            self._held_rebuilds.discard(hold)
            # CONDITIONAL: a timed-out rebuild on another thread may have STOLEN
            # ownership while we ran. Clearing it then would un-own the stealer and let
            # a job span fork straight into its live re-import. The pending count is
            # ours to drop either way, and it is what keeps job spans queued until the
            # last rebuild leaves.
            if self._owner == tid:
                self._owner = None
            self._pending_rebuilds -= 1
            self._cond.notify_all()

    def _wake(self) -> None:
        """Wake every thread waiting on the gate, so it re-reads its stop event.

        ``Condition.wait_for`` only re-evaluates its predicate on a notify (or at the
        timeout), so a stop event set without a notify under ``_cond`` would leave a waiting
        thread asleep for its full budget.
        """
        with self._cond:
            self._cond.notify_all()

    @contextmanager
    def exclusive(self, *, timeout: float) -> Iterator[None]:
        """Hold the rebuilding side for the span of a module re-import.

        Registers the intent first (so new job spans queue behind it), then waits
        (bounded by ``timeout``) for every open job span to close, but the one open in
        the calling context, and for any other thread's rebuild to finish. A wait that exceeds the budget logs at
        ERROR — naming what actually blocked — and proceeds anyway: a runaway job
        must never block a reload forever. Re-entrant: a nested call on the owning
        thread passes straight through. A timed-out proceed STEALS ownership; see the
        class docstring for what that degrades.

        The body runs in a context marked with this hold, so a fork the body makes is
        the rebuild's own fork (see :meth:`guard_forks`); a nested call keeps the outer mark.
        """
        tid = threading.get_ident()
        hold = object()
        disposition: list[str] = []
        try:
            self._enter_exclusive(tid, hold, _job_span_mark.get(), timeout, None, disposition)
            if disposition[0] == self._REENTRANT:
                yield
            else:
                with self._marked_as(hold):
                    yield
        finally:
            # Only a call that registered releases: a nested one never took the hold.
            if disposition and disposition[0] != self._REENTRANT:
                self._leave_exclusive(tid, hold)

    @staticmethod
    @contextmanager
    def _marked_as(hold: object) -> Iterator[None]:
        """Run the enclosed body in this context marked as the body of the rebuild ``hold``."""
        mark = _rebuild_hold.set(hold)
        try:
            yield
        finally:
            _rebuild_hold.reset(mark)

    @asynccontextmanager
    async def exclusive_async(self, *, timeout: float) -> AsyncIterator[None]:
        """:meth:`exclusive` for a rebuild whose body is a coroutine on the serving loop.

        The hold is taken and released on ONE dedicated thread that does nothing but
        hold it, so ownership stays with a single thread exactly as the re-entrancy
        model requires. Acquiring on a pooled ``to_thread`` worker and releasing on
        another would leave ``_owner`` pointing at a thread that no longer holds
        anything. The loop is never blocked: the holder thread is awaited through
        :func:`~tai42_kit.thread_wait.hold_on_thread`, so the serving loop keeps running
        while the gate waits for job spans to drain, and the release is observed before
        this returns.

        Timeout semantics are :meth:`exclusive`'s, unchanged — the holder thread logs
        at ERROR and proceeds, so the body always runs.

        Cancellable at BOTH points: a cancellation landing mid-ACQUIRE wakes the holder,
        which abandons the acquire and takes no hold, instead of waiting out the full
        budget; a cancellation landing during the BODY releases on the way out like any
        other exit.

        The body runs in the awaiting task's context, marked with this hold as
        :meth:`exclusive` marks its own.
        """
        hold_id = object()
        # Read here: the holder thread does not run in the awaiting task's context.
        own_span = _job_span_mark.get()
        disposition: list[str] = []

        def acquire(stop: threading.Event) -> bool:
            self._enter_exclusive(threading.get_ident(), hold_id, own_span, timeout, stop, disposition)
            return disposition[0] != self._ABORTED

        def release() -> None:
            # Only an acquire that registered releases; it registered from this same thread.
            if disposition and disposition[0] != self._REENTRANT:
                self._leave_exclusive(threading.get_ident(), hold_id)

        async with hold_on_thread(acquire, release, wake=self._wake, name="tai-fork-gate-hold"):
            with self._marked_as(hold_id):
                yield


def _report_span_overrun(timeout: float) -> None:
    """Report a job span that entered only because its wait on a re-import ran out."""
    logger.error(
        "fork gate: a job span could not enter within %.1fs while a module re-import is in "
        "progress; proceeding anyway — a job run in the span may import against a half-torn "
        "module table, and a child forked in it may deadlock on an inherited import lock",
        timeout,
    )


# The one process-wide gate. A work loop takes ``job_span``, or ``job_span_async`` for
# a coroutine job (and a forking one installs ``guard_forks``); the reload
# gate takes ``exclusive`` around every heavy reload body, and a serving-loop
# rebuild takes ``exclusive_async``.
fork_gate = ForkGate()
