"""Errors raised by the monitoring contract."""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class MonitoringError(Exception):
    """Base class for monitoring-contract errors."""

    # A bare monitoring failure is an unclassified fault of the monitoring backend
    # the caller delegates to (concrete subclasses stamp their own specific kind).
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR


class TraceNotFoundError(MonitoringError):
    """A trace id is absent on the backend.

    Vendor-neutral so consumers branch on type, not vendor error prose.
    Distinct from a transient backend-health failure, which ``get_trace``
    surfaces by letting the underlying error propagate (never by returning
    ``None``).
    """

    # The requested trace id is absent on the backend.
    __tai_error_kind__ = ErrorKind.NOT_FOUND


class MonitoringReadNotSupportedError(MonitoringError):
    """A read method was called on a backend that cannot read.

    Every backend exposes a ``reader`` object, but a write-only backend's read
    methods raise this on call (the property access still returns an object).
    Raised only by a backend that cannot read at all; a request outside a reading
    backend's declaration is :class:`MonitoringReadNotServedError`.
    """

    # A capability the backend does not provide — mirrors the skeleton's
    # NotSupportedError -> UNAVAILABLE (a missing capability, not a bad call).
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class MonitoringReadTooBroadError(MonitoringError):
    """A read request is too broad for the backend to resolve, not a missing capability.

    The backend CAN read; the request asks over too wide a window or too deep a
    page for it to answer within its bounded budget (a metric-ranked page the
    listing walk cannot cover, a window whose token or error enrichment overruns
    the page cap). The caller's fault, so the caller narrows it — a tighter time
    range or a shallower page. Distinct from
    :class:`MonitoringReadNotSupportedError` (the backend cannot read at all).
    Carries the backend's human guidance (e.g. "narrow the time range") verbatim.
    """

    # The caller's request was too broad — a client input fault, not a backend
    # capability gap (maps to a 4xx the caller corrects, never a 501).
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class MonitoringReadNotServedError(MonitoringError):
    """The backend reads, but this request asks for something outside what it declares it serves.

    A sort it does not list, a filter a declared sort cannot combine with, a measure,
    dimension, granularity or order field it does not serve. The caller changes the
    request; a different backend is not needed. The message names what was asked and
    what the backend serves.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class ObservationNotFoundError(MonitoringError):
    """An observation id is absent from a trace the backend holds."""

    __tai_error_kind__ = ErrorKind.NOT_FOUND


class PayloadRefUnresolvedError(MonitoringError):
    """The backend does not hold, at the time of the read, a value a reference names.

    The origin record is not yet available (the backend's ingestion is asynchronous) or
    was lost, or the pointer is absent in it. An upstream fault, never rendered as an
    empty value; every message ends with "(not yet available or lost)" so a reader knows
    a retry may succeed.
    """

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR
