"""The process-wide reload gate — the mutual-exclusion seam between an admin config reload and the live serving surface.

A reload (`admin.reload_config` / `admin.reload_mcp` / the failed-MCP re-probe)
is heavy and synchronous: it re-reads env, resets settings caches, re-imports
the manifest modules and re-probes every MCP server. Running it inline on the
serving loop freezes health checks, MCP keepalives and in-flight SSE streams for
its whole duration. The gate replaces that freeze:

* :meth:`ReloadGate.run` holds an :class:`asyncio.Lock` and awaits the heavy sync
  body on a worker thread (`asyncio.to_thread`), so the serving loop keeps
  running while the reload executes;
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

A call of an SSE session acts for its own admission record, not the session stream's
(:func:`~tai42_skeleton.app.admission.admit_session_call`), so it is recorded exactly as a
request is and its record finishes once its reply is handed to the transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from starlette.responses import JSONResponse
from tai42_kit.fork_gate import fork_gate

if TYPE_CHECKING:
    from tai42_skeleton.app.admission import RequestAdmission
    from tai42_skeleton.app.epoch import Epoch

T = TypeVar("T")

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
#   rq's queue default job timeout (``rq.Queue.DEFAULT_TIMEOUT``, 180s — the enqueue path
#   sets no ``job_timeout``) is the NOMINAL bound, six times this budget, but on these
#   pools it is not reliably enforceable: off the main thread the timer death penalty
#   cannot interrupt a C-blocked call, so a job wedged in one runs past it. The degraded
#   proceed below is what actually bounds this side.
#
# A reload therefore proceeds UNGUARDED — logging the fork gate's ERROR — in exactly two
# residual cases: a solo/gevent job that has already been running longer than 30s, or a
# fork instant that happens to collide with the tail of an exhausted quiesce. In both the
# child is exposed to the re-import race the gate otherwise closes. That
# is the accepted tradeoff: the alternative is a reload that misses its fleet ack (and, on
# a sibling, blocks fleet convergence) behind a long-running job.
#
# (``RQ_TASK_TIMEOUT`` / ``rq_settings().task_timeout`` is NOT the relevant bound — it
# caps how long the enqueuing caller waits for a result, not how long the job executes.)
FORK_QUIESCE_SECONDS = 30.0


@dataclass(eq=False)
class GateParty:
    """The work that is about to wait on, then hold, the reload gate, as the generation records it.

    ``request`` is the admission record of the request it acts for (``None`` off a served
    request); ``task`` is the tool-run supervisor whose run it belongs to (its own task, or
    a child task of it such as an agent's tool call) and ``task_epoch`` the generation that
    admitted that run (both ``None`` outside a run of the live generation). A party with
    neither records nothing (the bus subscription, a periodic loop, the retire's own tail).
    """

    request: RequestAdmission | None
    task: asyncio.Task[Any] | None
    task_epoch: Epoch | None

    def wait(self) -> None:
        """Record the party as waiting on the gate; its request leaves the in-flight count while it waits."""
        if self.request is not None:
            self.request.begin_wait()
        if self.task is not None and self.task_epoch is not None:
            self.task_epoch.begin_task_wait(self.task)

    def stop_waiting(self) -> None:
        """Record that the party no longer waits (it acquired the gate, or gave up)."""
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
    """The current request and tool-run supervisor, for the reload gate to record on their generation."""
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

    async def _acquire(self, party: GateParty) -> asyncio.Lock:
        """Acquire the serving-loop lock with ``party`` recorded as waiting while it waits."""
        lock = self._serving_lock()
        party.wait()
        try:
            await lock.acquire()
        finally:
            party.stop_waiting()
        return lock

    @asynccontextmanager
    async def driving(self) -> AsyncIterator[None]:
        """Hold the gate to run a gated body, the current request or supervisor recorded as its driver.

        Use ``async with reload_gate.driving(): ...`` around a body that rebuilds or swaps the
        serving generation outside :meth:`run` (the profile apply).
        """
        party = gate_party()
        lock = await self._acquire(party)
        try:
            party.drive()
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def synchronized(self) -> AsyncIterator[None]:
        """Hold the gate without driving a reload: wait one out, or read under the lock.

        Use ``async with reload_gate.synchronized(): ...``.
        """
        lock = await self._acquire(gate_party())
        try:
            yield
        finally:
            lock.release()

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
