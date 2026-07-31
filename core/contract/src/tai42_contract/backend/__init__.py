"""Execution-backend contract: the ``Backend`` ABC + the ``CallbackSchema``."""

from __future__ import annotations

from tai42_contract.backend.base import Backend
from tai42_contract.backend.callback import CallbackSchema

__all__ = ["Backend", "CallbackSchema"]
