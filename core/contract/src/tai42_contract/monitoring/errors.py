"""Errors raised by the monitoring contract."""

from __future__ import annotations


class MonitoringError(Exception):
    """Base class for monitoring-contract errors."""


class TraceNotFoundError(MonitoringError):
    """A trace id is absent on the backend.

    Vendor-neutral so consumers branch on type, not vendor error prose.
    Distinct from a transient backend-health failure, which ``get_trace``
    surfaces by letting the underlying error propagate (never by returning
    ``None``).
    """


class MonitoringReadNotSupportedError(MonitoringError):
    """A read method was called on a backend that cannot read.

    Every backend exposes a ``reader`` object, but a write-only backend's read
    methods raise this on call (the property access still returns an object).
    """
