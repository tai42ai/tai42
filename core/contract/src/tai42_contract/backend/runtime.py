"""One launchable runtime of an execution backend.

``Backend.launch`` receives a subcommand and its options; this module declares
what ONE such subcommand is, so the vendor binding answers only "how MY engine
starts, drains, and recycles" while the host owns readiness gating, signal
wiring, drain-on-cancel, pool turnover and the liveness signal. Declarations
only — every default body either raises (a declaration the binding did not
honor) or does nothing — plus the progress seam the host binds and a loop
runtime reports through.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, Protocol, Self

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ExecutionMode(StrEnum):
    """How a runtime's run body must be driven."""

    on_loop = "on_loop"
    """The run body is a coroutine driven by the serving loop."""

    worker_thread = "worker_thread"
    """The run body blocks; the host runs it on a dedicated daemon thread."""

    inline = "inline"
    """The run body blocks the serving loop deliberately; the host wires nothing."""


CONSUMING_RUNTIME: Final[str] = "worker"
"""Canonical launch-subcommand name of the runtime that pulls queued work.

The host exports the resolved manifest into the env only for this runtime: the
CLI sniffs argv BEFORE the plugin module is imported, so it cannot ask the
backend. A backend that names its consuming runtime anything else does not get
the manifest export."""


class FleetOpEffect(StrEnum):
    """What applying a worker-bus op changed in this process — the vocabulary a post-apply handler decides on."""

    registry = "registry"
    """This process's tool registry changed."""

    templates = "templates"
    """This process's compiled-template cache was dropped."""


class FleetOpApplied(BaseModel, frozen=True):
    """A worker-bus op that applied in this process, as a post-apply handler receives it.

    ``effects`` is what the op changed here (empty for a query or a process exit); a handler
    decides on it, never on ``op``, which names the op for logs and a turnover reason.
    ``budget`` is the seconds left before the publisher's report cut, measured from this
    process's receipt of the op; a post-apply obligation that cannot finish within it must raise.
    """

    op: str
    effects: frozenset[FleetOpEffect]
    budget: float


@dataclass(frozen=True)
class ProgressLiveness:
    """The process is alive while its engine loop keeps reporting progress.

    The host binds a reporter before ``build`` (:meth:`BackendRuntime.bind_progress`); the
    runtime calls :meth:`BackendRuntime.report_progress` from inside its loop on every pass.
    """


@dataclass(frozen=True)
class HttpLiveness:
    """The process is alive while its HTTP server answers ``GET path`` with a success status on the port it serves.

    ``path`` is absolute (starts with ``/``) and needs neither a session nor data from a backing
    store, so the answer depends on the server alone.
    """

    path: str


Liveness = ProgressLiveness | HttpLiveness
"""How a backend process signals it is alive: by its loop's progress, or by its HTTP server."""


class ProgressReporter(Protocol):
    """The host's receiver of a loop runtime's progress reports."""

    def __call__(self, *, next_within: float) -> None:
        """Record one pass of the loop, whose next pass is due within ``next_within`` seconds."""
        ...


class BackendRuntime(ABC):
    """ONE launchable runtime of an execution backend, named by its launch subcommand.

    The vendor binding answers only "how MY engine starts, drains, and
    recycles"; readiness gating, signal wiring, drain-on-cancellation and pool
    turnover belong to the host base that drives this object. The liveness
    signal is the host's too: the runtime declares which kind it has
    (:attr:`liveness`), and a progress runtime reports from its own loop.
    """

    name: ClassVar[str]
    """The launch subcommand that selects this runtime."""

    mode: ClassVar[ExecutionMode]
    """How the host must drive the run body."""

    consumes_work: ClassVar[bool] = False
    """True when this runtime pulls queued work. Only a consuming runtime is
    readiness-gated, drain-wired, and turnover-wired."""

    pool_turnover_required: bool = False
    """True when work executes in worker processes that PERSIST ACROSS JOBS and
    therefore hold a snapshot of this process's tool registry: a registry
    mutation does not reach them, so the pool must be turned over. False for a
    runtime that runs work in-process, and for one that spawns a fresh child per
    job (the next child inherits the mutation).

    Declared per class like the rest, but deliberately NOT a ``ClassVar``: a
    launch option can decide whether this engine's pool persists, so an instance
    may set it. The host therefore reads it off the live runtime."""

    liveness: ClassVar[Liveness]
    """How this process signals it is alive. REQUIRED: the host refuses a runtime without it.

    A consuming runtime declares :class:`ProgressLiveness`: what it is alive for is consuming,
    and its loop is what consumes. An HTTP server declares :class:`HttpLiveness` with the path
    an orchestrator's HTTP probe requests."""

    _progress: ProgressReporter | None = None

    # -- liveness ------------------------------------------------------------

    def bind_progress(self, report: ProgressReporter) -> None:
        """Bind the host's progress reporter: once, before :meth:`build`. A second bind raises ``RuntimeError``."""
        if self._progress is not None:
            raise RuntimeError(f"{type(self).__name__} already has a progress reporter bound")
        self._progress = report

    def report_progress(self, *, next_within: float) -> None:
        """Report one pass of this runtime's engine loop and promise the next one within ``next_within`` seconds.

        Called from inside the loop on every pass, idle or busy, from any thread. ``next_within``
        is the loop's own sleep or poll bound for the pass that follows; the probe judges this
        process against it, so a loop that stops passing stops reporting and is seen as such.
        Raises ``RuntimeError`` when no reporter is bound (a runtime driven without the host's
        binding, or one that declares :class:`HttpLiveness`) and ``ValueError`` when
        ``next_within`` is not positive.
        """
        if self._progress is None:
            raise RuntimeError(f"{type(self).__name__} reported progress with no reporter bound")
        if not (math.isfinite(next_within) and next_within > 0):
            raise ValueError(f"next_within must be positive seconds, got {next_within!r}")
        self._progress(next_within=next_within)

    # -- construction --------------------------------------------------------

    @classmethod
    @abstractmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Parse this runtime's own options and return the runtime.

        Strict — an unknown option aborts the launch loudly. Parses only; starts
        nothing.
        """

    async def build(self) -> None:
        """Construct engine objects.

        Runs BEFORE the readiness gate — building is not consuming — and before any
        run body. Default: nothing to build.

        Concrete rather than abstract: a runtime whose engine object is built by
        ``from_args`` alone must not be forced to declare an empty override.
        """
        return

    # -- running -------------------------------------------------------------

    async def run_on_loop(self) -> None:
        """Consume until stopped, driven by the serving loop.

        REQUIRED iff ``mode`` is ``on_loop``.
        """
        raise NotImplementedError(f"{type(self).__name__} declares mode=on_loop but implements no run_on_loop")

    def run_blocking(self) -> None:
        """Consume until stopped, blocking the calling thread.

        REQUIRED iff ``mode`` is ``worker_thread`` or ``inline``.
        """
        raise NotImplementedError(f"{type(self).__name__} declares a blocking mode but implements no run_blocking")

    # -- stopping ------------------------------------------------------------

    def request_drain(self) -> None:
        """Stop accepting new work, let in-flight work finish, and make the run body RETURN once drained.

        Called from the loop thread — while a ``worker_thread`` body runs on
        another thread — so it MUST NOT block and MUST NOT touch
        main-thread-only APIs (``signal.signal`` above all: the host owns the
        process's signal disposition and a vendor rebind destroys it).

        MUST BE IDEMPOTENT: the host calls it on signal AND again on the
        cancellation path. Escalation belongs to :meth:`request_terminate`,
        never to a second ``request_drain``.

        REQUIRED iff ``consumes_work``.
        """
        raise NotImplementedError(f"{type(self).__name__} consumes work but implements no request_drain")

    def request_terminate(self) -> None:
        """Cold stop: abandon in-flight work now. Called on a repeated signal.

        Default: report that this runtime declares no cold stop and let the warm
        drain continue — the supervisor's kill is the backstop.
        """
        logger.warning("%s declares no cold stop; the warm drain continues", type(self).__name__)

    # -- capability-conditional ----------------------------------------------

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        """Replace every worker that predates the mutation named by ``reason``, and CONFIRM it.

        On return, no pre-mutation worker may take new work; one still finishing the
        job it holds exits after it, and is not waited for. Raise loudly if it
        cannot be confirmed within ``budget`` seconds — the
        caller is inside a fleet-op apply, and a raise makes that op report
        ``failed`` rather than a false ``applied``.

        Called from the fleet-op hook, which the host wires BEFORE :meth:`build`
        so an op landing during a long boot is not missed. So this may run before
        the engine objects exist, and it runs CONCURRENTLY with the run body once
        they do. A no-op is the correct answer whenever there is no pool yet, or
        the live pool turns out not to hold a snapshot: the workers that do not
        exist cannot be stale, and the next ones are built after the mutation.

        REQUIRED iff ``pool_turnover_required``.
        """
        raise NotImplementedError(f"{type(self).__name__} requires pool turnover but implements no turn_over_pool")

    # -- teardown ------------------------------------------------------------

    async def aclose(self) -> None:
        """Release engine resources once the run body is done with them.

        Always awaited, on every exit path. Default: nothing to release.

        The run body has normally LEFT by then, but there is one exception the
        binding has to survive: a blocking body cannot be cancelled, so a
        cancellation that arrives while it is still on its thread abandons it
        there (the host reports this) and teardown runs anyway. Release what can
        be released without assuming the body let go.

        Concrete rather than abstract: a runtime that owns no resource must not
        be forced to declare an empty override.
        """
        return
