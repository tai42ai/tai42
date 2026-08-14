"""Execution-backend contract: the ``Backend`` ABC, the ``BackendRuntime`` ABC
one launch subcommand implements, and the ``CallbackSchema``."""

from __future__ import annotations

from tai42_contract.backend.base import Backend
from tai42_contract.backend.callback import CallbackSchema
from tai42_contract.backend.runtime import (
    BUS_APPLY_TIMEOUT_DEFAULT,
    BUS_APPLY_TIMEOUT_ENV,
    CONSUMING_RUNTIME,
    REGISTRY_MUTATING_FLEET_OPS,
    BackendRuntime,
    ExecutionMode,
)

__all__ = [
    "BUS_APPLY_TIMEOUT_DEFAULT",
    "BUS_APPLY_TIMEOUT_ENV",
    "CONSUMING_RUNTIME",
    "REGISTRY_MUTATING_FLEET_OPS",
    "Backend",
    "BackendRuntime",
    "CallbackSchema",
    "ExecutionMode",
]
