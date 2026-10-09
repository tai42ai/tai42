"""Execution-backend contract.

Exposes the ``Backend`` ABC, the ``BackendRuntime`` ABC one launch subcommand implements, and
the ``CallbackSchema``.
"""

from __future__ import annotations

from tai42_contract.backend.base import Backend
from tai42_contract.backend.callback import CallbackSchema
from tai42_contract.backend.runtime import (
    CONSUMING_RUNTIME,
    BackendRuntime,
    ExecutionMode,
    FleetOpApplied,
    FleetOpEffect,
)

__all__ = [
    "CONSUMING_RUNTIME",
    "Backend",
    "BackendRuntime",
    "CallbackSchema",
    "ExecutionMode",
    "FleetOpApplied",
    "FleetOpEffect",
]
