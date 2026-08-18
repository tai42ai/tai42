"""One launchable runtime of an execution backend.

``Backend.launch`` receives a subcommand and its options; this module declares
what ONE such subcommand is, so the vendor binding answers only "how MY engine
starts, drains, and recycles" while the host owns readiness gating, signal
wiring, drain-on-cancel and pool turnover. Declarations only — every default
body either raises (a declaration the binding did not honor) or does nothing.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar, Final, Self

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


REGISTRY_MUTATING_FLEET_OPS: Final[frozenset[str]] = frozenset(
    {
        "reload_config",
        "reload_mcp",
        "deregister_mcp",
        "reload_tool",
        "remove_tool",
        "reload_failed_mcps",
    }
)
"""Worker-bus ops that mutate this process's tool registry.

``list_failed_mcps`` is a query and ``recycle`` ends the process, so neither is
here. These are one input to :data:`POOL_TURNOVER_FLEET_OPS`, the set the turnover
gate actually reads."""


TEMPLATE_EVICTION_FLEET_OPS: Final[frozenset[str]] = frozenset(
    {
        "evict_template",
        "clear_template_cache",
    }
)
"""Worker-bus ops that drop this process's compiled-template cache.

A runtime whose worker processes render templates and PERSIST ACROSS JOBS hold their
own compiled cache; a store edit dispatched over the bus reaches only bus MEMBERS, so
those workers keep serving a stale compilation until the pool turns over. One input to
:data:`POOL_TURNOVER_FLEET_OPS`."""


POOL_TURNOVER_FLEET_OPS: Final[frozenset[str]] = REGISTRY_MUTATING_FLEET_OPS | TEMPLATE_EVICTION_FLEET_OPS
"""Worker-bus ops after which a pool that PERSISTS ACROSS JOBS must be turned over —
the superset of "mutates the tool registry" and "evicts the compiled-template cache".

A registry mutation leaves persisted workers holding a stale snapshot; a template
eviction leaves them holding a stale compilation; either way the fix is the same
canonical pool turnover. The turnover gate reads THIS set, so a runtime whose workers
hold a snapshot of the registry OR their own compiled cache turns its pool over after
one of these applies."""


BUS_APPLY_TIMEOUT_ENV: Final[str] = "TAI_BUS_APPLY_TIMEOUT"
"""Env var carrying the worker bus's apply window — the deadline by which an op
must report a terminal verdict.

A pool turnover runs INSIDE that window, so the host derives the turnover's
budget from this value and a confirm-or-raise lands before the publisher's
report cut. A backend-runtime process is reached by the env, not by the bus
settings object, so the NAME is the agreement."""

BUS_APPLY_TIMEOUT_DEFAULT: Final[float] = 30.0
"""The apply window when the env var is unset. Mirrors the host's own default;
a drift guard on the host side keeps the two in step."""


class BackendRuntime(ABC):
    """ONE launchable runtime of an execution backend, named by its launch
    subcommand.

    The vendor binding answers only "how MY engine starts, drains, and
    recycles"; readiness gating, signal wiring, drain-on-cancellation and pool
    turnover belong to the host base that drives this object.
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

    # -- construction --------------------------------------------------------

    @classmethod
    @abstractmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Parse this runtime's own options (strictly — an unknown option aborts
        the launch loudly) and return the runtime. Parses only; starts nothing."""

    async def build(self) -> None:
        """Construct engine objects. Runs BEFORE the readiness gate — building is
        not consuming — and before any run body. Default: nothing to build.

        Concrete rather than abstract: a runtime whose engine object is built by
        ``from_args`` alone must not be forced to declare an empty override."""
        return None

    # -- running -------------------------------------------------------------

    async def run_on_loop(self) -> None:
        """Consume until stopped, driven by the serving loop.

        REQUIRED iff ``mode`` is ``on_loop``."""
        raise NotImplementedError(f"{type(self).__name__} declares mode=on_loop but implements no run_on_loop")

    def run_blocking(self) -> None:
        """Consume until stopped, blocking the calling thread.

        REQUIRED iff ``mode`` is ``worker_thread`` or ``inline``."""
        raise NotImplementedError(f"{type(self).__name__} declares a blocking mode but implements no run_blocking")

    # -- stopping ------------------------------------------------------------

    def request_drain(self) -> None:
        """Stop accepting new work, let in-flight work finish, and make the run
        body RETURN once drained.

        Called from the loop thread — while a ``worker_thread`` body runs on
        another thread — so it MUST NOT block and MUST NOT touch
        main-thread-only APIs (``signal.signal`` above all: the host owns the
        process's signal disposition and a vendor rebind destroys it).

        MUST BE IDEMPOTENT: the host calls it on signal AND again on the
        cancellation path. Escalation belongs to :meth:`request_terminate`,
        never to a second ``request_drain``.

        REQUIRED iff ``consumes_work``."""
        raise NotImplementedError(f"{type(self).__name__} consumes work but implements no request_drain")

    def request_terminate(self) -> None:
        """Cold stop: abandon in-flight work now. Called on a repeated signal.

        Default: report that this runtime declares no cold stop and let the warm
        drain continue — the supervisor's kill is the backstop."""
        logger.warning("%s declares no cold stop; the warm drain continues", type(self).__name__)

    # -- capability-conditional ----------------------------------------------

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        """Replace every worker that predates the mutation named by ``reason``,
        and CONFIRM it: on return, no pre-mutation worker may still serve work.

        Raise loudly if it cannot be confirmed within ``budget`` seconds — the
        caller is inside a fleet-op apply, and a raise makes that op report
        ``failed`` rather than a false ``applied``.

        Called from the fleet-op hook, which the host wires BEFORE :meth:`build`
        so an op landing during a long boot is not missed. So this may run before
        the engine objects exist, and it runs CONCURRENTLY with the run body once
        they do. A no-op is the correct answer whenever there is no pool yet, or
        the live pool turns out not to hold a snapshot: the workers that do not
        exist cannot be stale, and the next ones are built after the mutation.

        REQUIRED iff ``pool_turnover_required``."""
        raise NotImplementedError(f"{type(self).__name__} requires pool turnover but implements no turn_over_pool")

    # -- teardown ------------------------------------------------------------

    async def aclose(self) -> None:
        """Release engine resources once the run body is done with them. Always
        awaited, on every exit path. Default: nothing to release.

        The run body has normally LEFT by then, but there is one exception the
        binding has to survive: a blocking body cannot be cancelled, so a
        cancellation that arrives while it is still on its thread abandons it
        there (the host reports this) and teardown runs anyway. Release what can
        be released without assuming the body let go.

        Concrete rather than abstract: a runtime that owns no resource must not
        be forced to declare an empty override."""
        return None
