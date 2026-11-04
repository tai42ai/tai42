"""The arq :class:`~tai42_kit.backend.ManagedBackend` implementation.

Declarations only: the one launch subcommand this backend accepts and the
runtime it selects. Everything host-shaped around that runtime — subcommand
dispatch, the boot-readiness gate, composed warm/cold shutdown wiring and
drain-on-cancellation — comes from the shared base.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import CONSUMING_RUNTIME, BackendRuntime
from tai42_kit.backend import DRAIN_MARGIN_SECONDS, ManagedBackend

from tai42_backend_arq.settings import arq_settings
from tai42_backend_arq.worker import ArqWorkerRuntime


class ArqBackend(ManagedBackend):
    """arq execution backend: the worker runtime that executes enqueued work."""

    label = "arq"
    runtimes: ClassVar[Mapping[str, type[BackendRuntime]]] = {CONSUMING_RUNTIME: ArqWorkerRuntime}

    @property
    def drain_timeout(self) -> float:
        """The base's drain budget, sized to THIS deployment's warm-drain window.

        arq's own ``job_completion_wait`` is how long it lets in-flight jobs
        finish, so the host's wait has to outlast it. Read live rather than frozen
        at import, so a settings epoch flip that widens the window widens the
        drain with it.
        """
        return float(arq_settings().job_completion_wait) + DRAIN_MARGIN_SECONDS


# Plain call (not decorator) so ``ArqBackend`` keeps its concrete class type.
tai42_app.backends.register_backend(ArqBackend)
