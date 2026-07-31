"""The :class:`RqBackend` — the strategy object that launches the RQ worker
runtime (worker/beat/dashboard) and executes the work its workers pull.

Registered on import via ``@tai42_app.backends.register_backend``. ``launch`` is
the sole :class:`~tai42_contract.backend.Backend` method.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from tai42_contract.app import tai42_app
from tai42_contract.backend import Backend


@tai42_app.backends.register_backend
class RqBackend(Backend):
    """RQ execution backend: ``launch`` runs worker/beat/dashboard."""

    async def launch(self, args: Sequence[str]) -> None:
        if not args:
            raise ValueError("RQ backend launch requires a command: worker|beat|dashboard")
        subcmd, *rest = args

        if subcmd == "worker":
            from tai42_backend_rq.worker import main, run_rq_worker

            ctx = main.make_context("rq-worker", list(rest))
            await run_rq_worker(**ctx.params)

        elif subcmd == "beat":
            sys.argv = ["rq-scheduler", *rest]
            from rq_scheduler.scripts.rqscheduler import main as scheduler_main

            scheduler_main()

        elif subcmd == "dashboard":
            sys.argv = ["rq-dashboard", *rest]
            from rq_dashboard.cli import run as dashboard_main

            dashboard_main()

        else:
            raise ValueError(f"Unknown RQ backend command {subcmd!r}; expected worker|beat|dashboard")
