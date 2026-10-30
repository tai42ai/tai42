"""The process-wide reload gate — the mutual-exclusion seam between an admin config reload and the live serving surface.

A reload (`admin.reload_config` / `admin.reload_mcp` / the failed-MCP re-probe)
is heavy and synchronous: it re-reads env, resets settings caches, re-imports
the manifest modules and re-probes every MCP server. Run inline on the serving
loop, an MCP re-probe would freeze health checks, MCP keepalives and in-flight SSE
streams for its whole duration. The gate keeps the bodies off the loop:

* :meth:`ReloadGate.run` holds an :class:`asyncio.Lock` and awaits the heavy sync
  body on a worker thread (`asyncio.to_thread`). An MCP re-probe body runs there,
  marshalling only short reconciles onto the serving loop, so the loop keeps
  running while it executes. A config reload
  marshals its rebuild back onto the serving loop (`config_reload`): the proposed
  env and the cleared settings caches are process-wide, so the swap is atomic for
  live requests only while nothing is served, and health/readiness probes, MCP
  keepalives, in-flight streams and the generation's timers pause until the swap
  returns. The worker-bus heartbeat runs on its own thread and does not pause;
* while the lock is held, routes that dispatch a run or mutate the live
  tool/agent registries answer :meth:`reject_response` (HTTP 503, retriable), and
  the FastMCP session surface rejects tool calls the same way. Health, liveness
  and read-only GETs keep answering;
* a body declared ``reimports=True`` ALSO holds ``fork_gate.exclusive`` on that
  worker thread, which excludes the OTHER live consumer of this process's modules:
  a backend work loop that forks a job child. A re-importing body pops the manifest
  modules out of ``sys.modules`` and re-imports them (``import_or_reload_package``),
  and a child forked during that window inherits a held ``importlib`` per-module lock
  whose owner thread does not exist post-fork — it blocks forever on that import and
  wedges the work loop monitoring it. The asyncio lock cannot cover this: the forking
  work loop is off-loop and never touches it. ``reimports`` is REQUIRED and explicit
  at every call site rather than defaulted, so adding a door is a decision about the
  fork gate, not an omission.

Only ``reload_config`` re-imports. ``reload_mcp`` / ``deregister_mcp`` /
``reload_failed_mcps`` re-probe MCP servers and rebind tools against modules already
in ``sys.modules``; they declare ``reimports=False`` and do NOT stall a forking
backend's jobs, which matters because an MCP re-probe can take a while and job
throughput should not pay for it.

The lock is owned by the serving loop. A single process runs a single serving
loop, so the lock is created once (on first use, or explicitly via
:meth:`bind_to_running_loop` at lifespan startup) and bound to that loop for the
process lifetime; an off-serving-loop caller reaches the gate by scheduling a
coroutine onto the serving loop (``asyncio.run_coroutine_threadsafe``), never by
acquiring the lock from a foreign loop.

A forked child starts with the gate unbound and free. A backend pool forks its job
children off the serving loop at any moment, including while the parent holds the
gate; the child inherits that held lock, which only the parent's serving loop can
ever release, so the child would answer ``reloading`` to every gated call for its
whole life. The child runs no reload of its own (a reload in the parent reaches its
children by re-forking them), so nothing in it is ever reloading.

The lock is never taken bare. Every holder enters through one of two context managers,
which record on the serving generation the work engaged with the gate
(:class:`GateParty`), so a retire of that generation never severs it. A request waiting for
the lock cannot move, so it leaves the in-flight count while it waits:

* :meth:`ReloadGate.driving` — the holder runs a gated body (a reload, a profile apply).
  While it waits for the lock its request or tool-run supervisor is recorded as
  waiting; once it holds the lock it is recorded as driving until it finishes, and its
  request leaves the in-flight count: it cannot finish before the gate is released,
  which happens only after the retire it drives has returned.
* :meth:`ReloadGate.synchronized` — the holder only waits a reload out or reads under
  the lock (a tool lookup that missed mid-rebuild, a re-probe pass). It is recorded as
  waiting while it waits and is ordinary in-flight work again once it holds the lock.

Both acquire through :func:`hold_recorded`, as does any other lock whose holder may drive a
retire (the config-write lock a settings-profile apply holds across its build), so every such
waiter is recorded the same way. A blocking span taken off the loop (the config store's
exclusive write span) is entered through :func:`hold_recorded_blocking`, which records its
waiter the same way.

A call of an SSE session acts for its own admission record, not the session stream's
(:func:`~tai42_skeleton.app.admission.admit_session_call`), so it is recorded exactly as a
request is and its record finishes once its session stream has written its reply.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from starlette.responses import JSONResponse
from tai42_kit.fork_gate import fork_gate

if TYPE_CHECKING:
    from tai42_skeleton.app.admission import RequestAdmission
    from tai42_skeleton.app.epoch import Epoch

T = TypeVar("T")

logger = logging.getLogger(__name__)

# The single retriable-rejection message, shared by the HTTP 503 body and the
# FastMCP session-tool rejection so a client sees one consistent surface.
REJECT_MESSAGE = "reloading — the server is applying a config reload; retry shortly"

# How long a re-importing rebuild waits for a forking backend's in-flight job child to
# be reaped before it starts — the one budget for every door, shared with the
# profile-apply path that drives ``build_and_swap_epoch`` directly.
#
# This is a DELIBERATE bound against the fleet-ack deadline, not a wait sized to the
# work. How long a job span actually lasts depends on the pool:
#
# * PREFORK (the default): a span is ~the ``os.fork()`` instant — the child inherits a
#   snapshot of the import locks and is immune thereafter — so a reload's wait here is
#   normally milliseconds, however long the job itself runs.
# * SOLO / GEVENT: the job runs IN the worker process, so its span lasts the whole job.
#   rq's job timeout is the dispatch ``task_timeout`` (``RQ_TASK_TIMEOUT``, 300 s by
#   default), the NOMINAL bound, ten times this budget, but on these pools it is not
#   reliably enforceable: off the main thread the timer death penalty cannot interrupt a
#   C-blocked call, so a job wedged in one runs past it. The degraded proceed below is
#   what actually bounds this side.
#
# A reload therefore proceeds UNGUARDED — logging the fork gate's ERROR — in exactly two
# residual cases: a solo/gevent job that has already been running longer than 30s, or a
# fork instant that happens to collide with the tail of an exhausted quiesce. In both the
# child is exposed to the re-import race the gate otherwise closes. That
# is the accepted tradeoff: the alternative is a reload that misses its fleet ack (and, on
# a sibling, blocks fleet convergence) behind a long-running job.
FORK_QUIESCE_SECONDS = 30.0


@dataclass(eq=False)
class GateParty:
    """The work that is about to wait on, then hold, a lock whose holder may drive its generation's retire.

    The generation records it: the reload gate's parties, and those of every lock acquired
    through :func:`hold_recorded`. ``request`` is the admission record of the request it acts for (``None`` off a served
    request); ``task`` is the tool-run supervisor whose run it belongs to (its own task, or
    a child task of it such as an agent's tool call) and ``task_epoch`` the generation that
    admitted that run (both ``None`` outside a run of the live generation). A party with
    neither records nothing (the bus subscription, a periodic loop, the retire's own tail).
    """

    request: RequestAdmission | None
    task: asyncio.Task[Any] | None
    task_epoch: Epoch | None

    def wait(self) -> None:
        """Record the party as waiting on the lock; its request leaves the in-flight count while it waits."""
        if self.request is not None:
            self.request.begin_wait()
        if self.task is not None and self.task_epoch is not None:
            self.task_epoch.begin_task_wait(self.task)

    def stop_waiting(self) -> None:
        """Record that the party no longer waits (it acquired the lock, or gave up)."""
        if self.request is not None:
            self.request.end_wait()
        if self.task is not None and self.task_epoch is not None:
            self.task_epoch.end_task_wait(self.task)

    def drive(self) -> None:
        """Record the party as driving a gated body until its request or supervisor finishes.

        A driving request leaves the in-flight count (its slot is released once): it cannot
        finish before the gate is released, which happens only after a retire it drives has
        returned, so the retire waits for it apart from the drain budget.
        """
        if self.request is not None:
            self.request.epoch.driving.add(self.request)
            self.request.release_slot()
        if self.task is not None and self.task_epoch is not None and not self.task.done():
            epoch = self.task_epoch
            epoch.driving_tasks.add(self.task)
            self.task.add_done_callback(epoch.driving_tasks.discard)


def gate_party() -> GateParty:
    """The current request and tool-run supervisor, for a recorded lock wait to record on their generation."""
    from tai42_skeleton.app.admission import current_request_admission
    from tai42_skeleton.app.epoch import current_epoch_or_none
    from tai42_skeleton.operations.tool_runs.supervisor import running_supervisor

    supervisor = running_supervisor()
    task: asyncio.Task[Any] | None = None
    task_epoch: Epoch | None = None
    if supervisor is not None:
        live = current_epoch_or_none()
        if live is not None and live.number == supervisor[1]:
            task, task_epoch = supervisor[0], live
    return GateParty(request=current_request_admission(), task=task, task_epoch=task_epoch)


@asynccontextmanager
async def hold_recorded(lock: asyncio.Lock, party: GateParty | None = None) -> AsyncIterator[GateParty]:
    """Hold ``lock``, with ``party`` (the current one by default) recorded as waiting while it waits.

    For a lock whose holder may drive a retire of the waiter's generation: a waiter cannot move
    before the holder releases, so the retire never counts it against the drain budget and never
    severs it. Yields the party, released on exit.
    """
    party = gate_party() if party is None else party
    party.wait()
    try:
        await lock.acquire()
    finally:
        party.stop_waiting()
    try:
        yield party
    finally:
        lock.release()


class _ThreadEntry:
    """A blocking sync span entered on a worker thread, closed by the thread itself once abandoned.

    The awaiting task can be cancelled while the thread still blocks on entry. The thread then
    finishes entering with no one left to exit the span, so :meth:`enter` exits it at once when
    :meth:`abandon` came first; when the entry finished first, :meth:`abandon` reports it and the
    caller exits. An entry that fails after :meth:`abandon` is logged, as no awaiter is left to
    receive it; one that fails first is kept in :attr:`error` for the caller, whose cancellation
    may have cancelled the future that carried it.
    """

    def __init__(self, span: AbstractContextManager[None]) -> None:
        self._span = span
        self.stack = ExitStack()
        self._guard = threading.Lock()
        self._entered = False
        self._abandoned = False
        self.error: BaseException | None = None

    def enter(self) -> None:
        try:
            self.stack.enter_context(self._span)
        except BaseException as error:
            with self._guard:
                abandoned = self._abandoned
                if not abandoned:
                    self.error = error
            if abandoned:
                # The awaiting task is gone, so nothing can receive this error but the log.
                logger.exception("entering a blocking span failed after its waiter was cancelled")
            raise
        with self._guard:
            if not self._abandoned:
                self._entered = True
                return
        try:
            self.stack.close()
        except BaseException:
            # The awaiting task is gone, so nothing can receive this error but the log.
            logger.exception("exiting a blocking span entered after its waiter was cancelled failed")
            raise

    def abandon(self) -> bool:
        """Mark the entry abandoned; return whether the span was already entered (the caller exits it).

        From here on :attr:`error` no longer changes: it holds the entry's error if the entry
        failed first, else ``None``.
        """
        with self._guard:
            self._abandoned = True
            return self._entered


async def _exit_on_thread(entry: _ThreadEntry) -> None:
    """Exit the entered span on a worker thread, returning only once the exit has completed.

    A cancellation that lands meanwhile is held until the exit is done and then re-raised; an
    error from the exit propagates instead, with the held cancellation as its ``__context__``.
    """
    # A plain executor future rather than a task: a loop teardown cancels every task, which would
    # leave the exit running unawaited and lose its error.
    exiting = asyncio.get_running_loop().run_in_executor(None, contextvars.copy_context().run, entry.stack.close)
    cancelled: asyncio.CancelledError | None = None
    while not exiting.done():
        try:
            # ``wait`` neither cancels the exit nor raises its error: it only returns once it is done.
            await asyncio.wait((exiting,))
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
    try:
        exiting.result()
    except BaseException as error:
        if cancelled is not None:
            error.__context__ = cancelled
        raise
    if cancelled is not None:
        raise cancelled


@asynccontextmanager
async def hold_recorded_blocking(span: AbstractContextManager[None], party: GateParty) -> AsyncIterator[None]:
    """Hold the blocking sync ``span`` on a worker thread, with ``party`` recorded as waiting while it blocks.

    The off-loop counterpart of :func:`hold_recorded`, for a span whose entry blocks the thread
    until another holder, possibly in another process, leaves it. The serving loop keeps running
    while it blocks, and the party is recorded as waiting exactly as a :func:`hold_recorded`
    waiter is. A waiter abandoned before its entry completed returns at once and leaves the entry
    to its thread, which exits the span as soon as entry completes; a waiter whose cancellation
    lands as its entry completes exits the span itself before it returns. The span must therefore
    exclude on its own and tolerate being entered and exited on different threads. The exit always
    completes before the holder returns: a cancellation during it is re-raised afterwards, and an
    error entering or exiting the span propagates; an entry that fails after its waiter returned
    is logged, as no one is left to receive it.
    """
    entry = _ThreadEntry(span)
    party.wait()
    try:
        await asyncio.to_thread(entry.enter)
    except BaseException as stopped:
        if entry.abandon():
            await _exit_on_thread(entry)
        elif entry.error is not None and entry.error is not stopped:
            # The entry failed, but the cancellation reached the waiter instead of the error.
            raise entry.error from stopped
        raise
    finally:
        party.stop_waiting()
    try:
        yield
    finally:
        await _exit_on_thread(entry)


class ReloadGate:
    """Process-wide reload lock plus its retriable-rejection response.

    Instantiated once per process (the module-level :data:`reload_gate`
    singleton). The lock is bound to the serving loop; because a process runs a
    single serving loop it is created once and reused for the process lifetime.
    """

    def __init__(self) -> None:
        """Create the gate with its lock unbound until first serving-loop use."""
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _serving_lock(self) -> asyncio.Lock:
        """The lock bound to the running serving loop, created on first use.

        A process runs a single serving loop, so this binds exactly once. It
        rebinds only if the running loop differs from the bound one — the loop
        was replaced (there is never a second live loop in the same process to
        share the lock with), which keeps the process-wide singleton correct
        across a loop swap rather than raising the cross-loop ``RuntimeError`` an
        ``asyncio.Lock`` would.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    def bind_to_running_loop(self) -> None:
        """Bind the lock to the current running loop.

        The lifespan-startup hook, so the gate is owned by the serving loop from
        the first request rather than lazily on the first reload.
        """
        self._serving_lock()

    @asynccontextmanager
    async def driving(self) -> AsyncIterator[None]:
        """Hold the gate to run a gated body, the current request or supervisor recorded as its driver.

        Use ``async with reload_gate.driving(): ...`` around a body that rebuilds or swaps the
        serving generation outside :meth:`run` (the profile apply).
        """
        async with hold_recorded(self._serving_lock()) as party:
            party.drive()
            yield

    @asynccontextmanager
    async def synchronized(self) -> AsyncIterator[None]:
        """Hold the gate without driving a reload: wait one out, or read under the lock.

        Use ``async with reload_gate.synchronized(): ...``.
        """
        async with hold_recorded(self._serving_lock()):
            yield

    @property
    def locked(self) -> bool:
        """Whether a reload is in progress.

        ``locked()`` reads a plain flag and never touches the loop, so a route
        entry check is safe before the lock has ever been bound (no reload has
        run yet).
        """
        return self._lock is not None and self._lock.locked()

    async def run(self, fn: Callable[[], T], *, reimports: bool) -> T:
        """Hold the reload lock through :meth:`driving` and await ``fn`` on a worker thread.

        The heavy reload bodies stay synchronous and unchanged; only the acquire
        and the thread offload live here, so the serving loop keeps running while
        the reload executes.

        ``reimports`` declares whether ``fn`` pops and re-imports the manifest modules.
        Only then is the fork gate taken — on the worker thread, around ``fn`` itself,
        so it spans the whole body including the loop-affine build a body marshals back
        onto the serving loop, which is where the re-import runs. A non-re-importing
        body (an MCP re-probe/rebind) must NOT hold it: it would stall a forking
        backend's jobs for the probe's duration to guard against a race it cannot cause.
        Required, never defaulted — adding a door is a decision about the fork gate.
        """
        async with self.driving():
            if not reimports:
                return await asyncio.to_thread(fn)
            return await asyncio.to_thread(self._run_fork_exclusive, fn)

    @staticmethod
    def _run_fork_exclusive(fn: Callable[[], T]) -> T:
        """Run one re-importing reload body with no backend child forked and no in-process job running.

        This is the invariant :data:`tai42_kit.fork_gate.fork_gate` exists to
        hold (stated canonically there).
        """
        with fork_gate.exclusive(timeout=FORK_QUIESCE_SECONDS):
            return fn()

    def reset_after_fork(self) -> None:
        """Start a forked child with the gate unbound: it inherits no hold of the parent's serving loop."""
        self._lock = None
        self._loop = None

    def reject_response(self) -> JSONResponse:
        """Return the retriable 503 a gated route serves while a reload holds the lock.

        Explicit and loud so a client/CLI can branch on ``reloading``.
        """
        return JSONResponse(
            {"error": REJECT_MESSAGE, "reloading": True},
            status_code=503,
            headers={"Retry-After": "5"},
        )


# The one process-wide gate. Routers import this singleton and check
# ``reload_gate.locked`` at entry; reload callers await ``reload_gate.run(...)``.
reload_gate = ReloadGate()
if sys.platform != "win32":
    os.register_at_fork(after_in_child=reload_gate.reset_after_fork)
