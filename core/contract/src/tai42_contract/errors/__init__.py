"""Shared exception types for the TAI ecosystem."""

from __future__ import annotations


class ClientDisconnectedError(Exception):
    """Raised when a client disconnects unexpectedly and is removed from the cache.

    This is not a permanent failure — retrying the operation will cause a fresh
    client to be created automatically.
    """


class ClientConnectError(ClientDisconnectedError):
    """Raised when a pooled client could not be (re)built — connect/init failed; a
    subclass so unavailable-client handling catches both."""


__all__ = ["ClientConnectError", "ClientDisconnectedError"]
