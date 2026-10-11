"""Errors the conversations feature raises that a component outside the platform core catches by type."""

from __future__ import annotations

from tai42_contract.errors import UnavailableError

__all__ = ["ThreadQueueOverflowError"]


class ThreadQueueOverflowError(UnavailableError):
    """The per-thread FIFO is full — a loud, retriable 503 rather than an unbounded backlog."""
