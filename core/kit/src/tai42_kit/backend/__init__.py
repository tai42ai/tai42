"""The execution-backend launch lifecycle, implemented once for every backend.

A vendor binding declares its runtimes and implements
:class:`~tai42_contract.backend.runtime.BackendRuntime` — how ITS engine starts,
drains, and recycles. Everything host-shaped around that (subcommand dispatch,
boot-readiness gating, off-loop running, composed warm/cold shutdown wiring,
drain-on-cancellation, pool turnover after a registry-mutating fleet op) lives
here, in :class:`~tai42_kit.backend.base.ManagedBackend`.

It lives in kit and not in the skeleton because a backend plugin may not import
the skeleton, and not in the contract because the contract carries no logic.
Kit is the only package both a plugin and the skeleton may import.

This subpackage is the one part of kit that reaches the running app: it imports
``tai42_contract.app.tai42_app`` for the readiness latch and the fleet-op hook.
That stays inside the leaf rule (the contract is kit's only tai dependency and
the handle is dependency-free), and ``tai42_kit`` re-exports nothing, so
``import tai42_kit`` still pulls nothing new.
"""

from tai42_kit.backend.base import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    DEFAULT_READY_TIMEOUT_SECONDS,
    ManagedBackend,
)
from tai42_kit.backend.conformance import check_backend_declarations, check_runtime_declarations
from tai42_kit.backend.settings import BackendDispatchSettings

__all__ = [
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_READY_TIMEOUT_SECONDS",
    "BackendDispatchSettings",
    "ManagedBackend",
    "check_backend_declarations",
    "check_runtime_declarations",
]
