"""The :class:`RqBackend` — RQ's binding of the shared launch lifecycle.

Registered on import via ``@tai42_app.backends.register_backend``. It declares
three runtimes (``worker`` / ``beat`` / ``dashboard``) and nothing else:
subcommand dispatch, boot-readiness gating, off-loop running, composed
warm/cold shutdown wiring and drain-on-cancellation all come from
:class:`~tai42_kit.backend.ManagedBackend`, whose ``launch`` is the sole
:class:`~tai42_contract.backend.Backend` method and is never overridden here.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode
from tai42_kit.backend import ManagedBackend

from tai42_backend_rq.settings import rq_settings
from tai42_backend_rq.worker import RqWorkerRuntime

# Margin the drain budget adds on top of the engine's own job budget: the warm
# drain has to outlast the longest in-flight job AND the engine's own teardown, or
# the wait is abandoned on exactly the work it exists to protect.
_DRAIN_MARGIN_SECONDS = 30.0


class _VendorCliRuntime(BackendRuntime):
    """A vendor CLI entrypoint, run INLINE on the serving loop.

    Beat and the dashboard pull no queued work, so there is nothing to
    readiness-gate and nothing to drain; they block the loop deliberately and
    keep whatever signal disposition their own CLI installs. That is a decision,
    not an oversight: nothing else runs on their loop, so there is no host
    handler for a vendor install to destroy.

    Their options belong to the vendor CLI, so they are handed through as argv
    rather than parsed here.
    """

    mode = ExecutionMode.inline

    argv0: ClassVar[str]
    """The program name the vendor CLI expects to find in ``sys.argv[0]``."""

    def __init__(self, args: Sequence[str]) -> None:
        self._args = list(args)

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls(args)

    def _publish_argv(self) -> None:
        """Hand this runtime's options to the vendor CLI the only way it reads
        them — the process argv."""
        sys.argv = [self.argv0, *self._args]


class RqBeatRuntime(_VendorCliRuntime):
    """``rq-scheduler``: moves due scheduled jobs onto the queue. It enqueues
    work rather than pulling it, so it is not a consuming runtime."""

    name = "beat"
    argv0 = "rq-scheduler"

    def run_blocking(self) -> None:
        self._publish_argv()
        # Imported on the path that runs it: only this runtime needs the
        # scheduler script, and only this process ever becomes one.
        from rq_scheduler.scripts.rqscheduler import main as scheduler_main

        scheduler_main()


class RqDashboardRuntime(_VendorCliRuntime):
    """``rq-dashboard``: the read-only web view over the queues."""

    name = "dashboard"
    argv0 = "rq-dashboard"

    def run_blocking(self) -> None:
        self._publish_argv()
        # Imported on the path that runs it: the dashboard drags in a whole web
        # stack that a worker or beat process must never pay for.
        from rq_dashboard.cli import run as dashboard_main

        dashboard_main()


@tai42_app.backends.register_backend
class RqBackend(ManagedBackend):
    """RQ execution backend: ``launch`` runs worker/beat/dashboard."""

    label = "rq"
    runtimes: ClassVar[Mapping[str, type[BackendRuntime]]] = {
        "worker": RqWorkerRuntime,
        "beat": RqBeatRuntime,
        "dashboard": RqDashboardRuntime,
    }

    @property
    def drain_timeout(self) -> float:
        """The base's drain budget, sized to THIS deployment's job budget.

        Read live rather than frozen at import, so a settings epoch flip that
        widens ``RQ_TASK_TIMEOUT`` widens the drain with it instead of leaving a
        warm shutdown abandoning a work-horse it should have waited for."""
        return float(rq_settings().task_timeout) + _DRAIN_MARGIN_SECONDS
