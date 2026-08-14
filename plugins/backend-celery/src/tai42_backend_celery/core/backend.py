"""The Celery execution backend: its launchable runtimes and their bindings.

Every Celery runtime is the same umbrella CLI under a different subcommand, so
each binding differs only in the argv it builds and in how it stops. ``worker``
blocks, so the host runs it on a worker thread and this process's asyncio loop
(which reads the app's worker-bus subscription) stays alive; ``beat`` and
``flower`` run nothing else on that loop, so they block it deliberately and keep
Celery's native signal handling.

Everything host-shaped around these bindings — subcommand dispatch, the
boot-readiness gate that keeps a pool child from forking against a half-built
tool registry, the composed SIGTERM/SIGINT wiring, drain-on-cancellation, and the
prefork-pool turnover after a registry-mutating bus op — belongs to
:class:`~tai42_kit.backend.ManagedBackend`, which drives these objects.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode
from tai42_kit.backend import BackendDispatchSettings, ManagedBackend

from tai42_backend_celery.core import prefork
from tai42_backend_celery.core.settings import celery_settings

# The Celery application module handed to the Celery CLI (`-A`).
_CELERY_APP_PATH = "tai42_backend_celery.core.app"

# Margin the drain budget adds on top of the engine's own job budget: the warm
# drain has to outlast the longest in-flight job AND the engine's own teardown, or
# the wait is abandoned on exactly the work it exists to protect.
_DRAIN_MARGIN_SECONDS = 30.0


def _celery_cli_main() -> None:
    """Run the Celery umbrella CLI against ``sys.argv``."""
    from celery.__main__ import main as celery_main

    celery_main()


class _CeleryRuntime(BackendRuntime):
    """What every Celery runtime shares: it parses nothing of its own (the
    options belong to the Celery CLI, which validates them) and starts by
    handing that CLI its own subcommand plus the rest of the launch argv."""

    def __init__(self, args: Sequence[str]) -> None:
        self._args = list(args)

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls(args)

    def _set_argv(self) -> None:
        """Point ``sys.argv`` at this runtime's Celery command — the CLI reads the
        process argv rather than taking arguments."""
        sys.argv = ["celery", "-A", _CELERY_APP_PATH, self.name, *self._args]


class CeleryWorkerRuntime(_CeleryRuntime):
    """The consuming runtime: a Celery worker whose prefork children persist
    across jobs, so a registry mutation obliges a pool turnover."""

    name = "worker"
    mode = ExecutionMode.worker_thread
    consumes_work = True
    pool_turnover_required = True

    async def build(self) -> None:
        """Set the CLI's argv and prepare the pool turnover, both before the host
        gates on boot-readiness: connecting Celery's setup/ready signals must
        happen before the worker boots past them, and neither consumes work."""
        self._set_argv()
        prefork.register()

    def run_blocking(self) -> None:
        _celery_cli_main()

    def request_drain(self) -> None:
        """Ask the in-process worker to stop warmly, by driving
        ``celery.worker.state`` directly: the worker runs off the main thread,
        where Celery skips its own signal handlers.

        Idempotent — a second request is the same request, and only a flag that
        was never set is set.
        """
        from celery.worker import state

        if state.should_stop is None:
            state.should_stop = 0

    def request_terminate(self) -> None:
        """Cold stop: abandon in-flight tasks now."""
        from celery.worker import state

        state.should_terminate = 0

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        """Re-fork this worker's prefork pool and confirm it, off the serving loop
        (the turnover's control I/O blocks). A raise fails the op named by
        ``reason``, which is the point: an unconfirmed turnover means a child may
        still serve the pre-mutation registry."""
        await asyncio.to_thread(prefork.turnover_local_pool, reason, budget)


class _CeleryInlineRuntime(_CeleryRuntime):
    """A Celery command that owns its process: nothing else runs on this loop, so
    it blocks the loop deliberately and keeps Celery's native signal handling."""

    mode = ExecutionMode.inline

    def run_blocking(self) -> None:
        self._set_argv()
        _celery_cli_main()


class CeleryBeatRuntime(_CeleryInlineRuntime):
    """The RedBeat scheduler."""

    name = "beat"


class CeleryFlowerRuntime(_CeleryInlineRuntime):
    """The Flower dashboard."""

    name = "flower"


class CeleryBackend(ManagedBackend):
    """Celery execution backend (see the module docstring for the design)."""

    label = "celery"
    runtimes: ClassVar[Mapping[str, type[BackendRuntime]]] = {
        "worker": CeleryWorkerRuntime,
        "beat": CeleryBeatRuntime,
        "flower": CeleryFlowerRuntime,
    }

    @property
    def dispatch_settings(self) -> BackendDispatchSettings:
        """The ``CELERY_`` env group; its ``manifest_key`` is the env var the
        re-forked pool children read the live manifest from."""
        return celery_settings()

    @property
    def drain_timeout(self) -> float:
        """The base's drain budget, sized to THIS deployment's Celery task budget.

        Read live rather than frozen at import, so a settings epoch flip that
        widens ``CELERY_TASK_TIMEOUT`` widens the drain with it instead of leaving
        a warm shutdown abandoning tasks it should have waited for."""
        return float(celery_settings().task_timeout) + _DRAIN_MARGIN_SECONDS


# Plain call (not decorator) so ``CeleryBackend`` keeps its plain class type.
tai42_app.backends.register_backend(CeleryBackend)
