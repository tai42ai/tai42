"""Shared exception types for the TAI ecosystem."""

from __future__ import annotations


class ClientDisconnectedError(Exception):
    """Raised when a client disconnects unexpectedly and is removed from the cache.

    This is not a permanent failure — retrying the operation will cause a fresh
    client to be created automatically.
    """


__all__ = ["ClientDisconnectedError"]
