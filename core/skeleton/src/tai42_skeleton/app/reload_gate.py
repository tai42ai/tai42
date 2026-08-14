"""The process-wide reload gate — the mutual-exclusion seam between an admin
config reload and the live serving surface.

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
acquiring the lock from a foreign loop. The raw lock is exposed via :attr:`lock`
for an async-side holder (a re-probe pass that must block reloads while it runs).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from starlette.responses import JSONResponse
from tai42_kit.fork_gate import fork_gate

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
# child is exposed to the re-import race exactly as it was before the gate existed. That
# is the accepted tradeoff: the alternative is a reload that misses its fleet ack (and, on
# a sibling, blocks fleet convergence) behind a long-running job.
#
# (``RQ_TASK_TIMEOUT`` / ``rq_settings().task_timeout`` is NOT the relevant bound — it
# caps how long the enqueuing caller waits for a result, not how long the job executes.)
FORK_QUIESCE_SECONDS = 30.0


class ReloadGate:
    """Process-wide reload lock plus its retriable-rejection response.

    Instantiated once per process (the module-level :data:`reload_gate`
    singleton). The lock is bound to the serving loop; because a process runs a
    single serving loop it is created once and reused for the process lifetime.
    """

    def __init__(self) -> None:
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
        """Bind the lock to the current running loop — the lifespan-startup hook so
        the gate is owned by the serving loop from the first request rather than
        lazily on the first reload."""
        self._serving_lock()

    @property
    def lock(self) -> asyncio.Lock:
        """The raw serving-loop lock, for an async-side holder that must block
        reloads while it runs (``async with reload_gate.lock: ...``)."""
        return self._serving_lock()

    @property
    def locked(self) -> bool:
        """Whether a reload is in progress. ``locked()`` reads a plain flag and
        never touches the loop, so a route entry check is safe before the lock has
        ever been bound (no reload has run yet)."""
        return self._lock is not None and self._lock.locked()

    async def run(self, fn: Callable[[], T], *, reimports: bool) -> T:
        """Hold the reload lock and await ``fn`` on a worker thread.

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
        async with self._serving_lock():
            if not reimports:
                return await asyncio.to_thread(fn)
            return await asyncio.to_thread(self._run_fork_exclusive, fn)

    @staticmethod
    def _run_fork_exclusive(fn: Callable[[], T]) -> T:
        """Run one re-importing reload body with no backend child being forked and no
        in-process job running — the invariant :data:`tai42_kit.fork_gate.fork_gate`
        exists to hold (stated canonically there)."""
        with fork_gate.exclusive(timeout=FORK_QUIESCE_SECONDS):
            return fn()

    def reject_response(self) -> JSONResponse:
        """The retriable 503 a gated route returns while a reload holds the lock —
        explicit and loud so a client/CLI can branch on ``reloading``."""
        return JSONResponse(
            {"error": REJECT_MESSAGE, "reloading": True},
            status_code=503,
            headers={"Retry-After": "5"},
        )


# The one process-wide gate. Routers import this singleton and check
# ``reload_gate.locked`` at entry; reload callers await ``reload_gate.run(...)``.
reload_gate = ReloadGate()
