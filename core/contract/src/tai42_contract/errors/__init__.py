"""Shared exception types and the stable failure taxonomy for the TAI ecosystem."""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    """The closed, transport-neutral failure taxonomy every layer classifies to.

    This is the transport-neutral twin of ``OperationError.status``: where the
    operations layer answers HTTP with a numeric status, ``ErrorKind`` names the
    SAME failure in one word an agent, a CLI, a channel, or any non-HTTP edge can
    branch on without re-deriving the classification from a status code or matching
    a message string.

    The member VALUES are the compatibility promise:

    * a value is NEVER renamed and NEVER removed — a persisted or wire-carried
      value keeps its meaning across versions;
    * ADDING a new member is a MINOR change — a consumer may meet a value its
      version does not know;
    * a consumer that meets an UNRECOGNISED value treats it as :attr:`UNKNOWN`
      (the closed set is a floor, never a frozen ceiling).

    Resolve any exception to its kind with :func:`error_kind`.
    """

    DELIVERY_FAILED = "delivery_failed"
    TIMED_OUT = "timed_out"
    BAD_INPUT = "bad_input"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UPSTREAM_ERROR = "upstream_error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# The well-known class/instance attribute an exception carries to STAMP its kind.
# A definition site sets ``__tai_error_kind__ = ErrorKind.X`` as a plain class
# attribute; :func:`error_kind` reads it first (instance attribute beats class
# attribute, subclasses inherit it through normal attribute lookup).
ERROR_KIND_ATTR = "__tai_error_kind__"

# The fallback registry: maps an exception CLASS to its kind for third-party
# classes that cannot be stamped at their definition site. Seeded below with the
# stdlib builtins only; :func:`error_kind` walks an exception's MRO over it so the
# MOST-DERIVED registered ancestor wins.
_ERROR_KIND_REGISTRY: dict[type[BaseException], ErrorKind] = {}

# Bound on the ``__cause__`` chain :func:`error_kind` follows before giving up — a
# typed cause five links deep is still recovered, a pathological/cyclic chain never
# spins.
_MAX_CAUSE_DEPTH = 5


def register_error_kind(exc_type: type[BaseException], kind: ErrorKind) -> None:
    """Register ``kind`` as the fallback classification for ``exc_type``.

    For a THIRD-PARTY exception class that cannot carry an :data:`ERROR_KIND_ATTR`
    stamp at its definition site (a vendor SDK's error, say). A stamp on the class
    or instance always wins over the registry; among registrations the most-derived
    ancestor wins. Registering an already-registered class overwrites it.
    """
    _ERROR_KIND_REGISTRY[exc_type] = ErrorKind(kind)


def _stamped_kind(exc: BaseException) -> ErrorKind | None:
    """The kind stamped on ``exc`` via :data:`ERROR_KIND_ATTR`, or ``None``.

    An instance attribute beats a class attribute (normal ``getattr`` order) and a
    subclass inherits a base's stamp. A value that is not a recognised member is a
    miss (a mistyped stamp never masquerades as a valid kind).
    """
    stamped = getattr(exc, ERROR_KIND_ATTR, None)
    if stamped is None:
        return None
    try:
        return ErrorKind(stamped)
    except ValueError:
        return None


def _registered_kind(exc: BaseException) -> ErrorKind | None:
    """The kind for the most-derived registered ancestor of ``type(exc)``."""
    for base in type(exc).__mro__:
        kind = _ERROR_KIND_REGISTRY.get(base)
        if kind is not None:
            return kind
    return None


def error_kind(exc: BaseException) -> ErrorKind:
    """Resolve ``exc`` to its stable :class:`ErrorKind`.

    The resolution order, each step returning on the first hit:

    1. a stamp on the instance or class (:data:`ERROR_KIND_ATTR`);
    2. the fallback :func:`register_error_kind` registry, walked over the MRO so the
       most-derived registered ancestor wins;
    3. the ``__cause__`` chain, up to :data:`_MAX_CAUSE_DEPTH` links — this recovers
       the typed error beneath a projection wrapper (e.g. the ``OperationError`` a
       projected ``ToolError`` is raised ``from``);
    4. :attr:`ErrorKind.UNKNOWN` when nothing classifies it.
    """
    current: BaseException | None = exc
    depth = 0
    while current is not None:
        stamped = _stamped_kind(current)
        if stamped is not None:
            return stamped
        registered = _registered_kind(current)
        if registered is not None:
            return registered
        if depth >= _MAX_CAUSE_DEPTH:
            break
        current = current.__cause__
        depth += 1
    return ErrorKind.UNKNOWN


# Seed the registry with the stdlib BUILTINS only — the ecosystem's own errors are
# stamped at their definition sites; these cover the standard exceptions a layer may
# let escape unstamped.
register_error_kind(TimeoutError, ErrorKind.TIMED_OUT)
register_error_kind(ConnectionError, ErrorKind.UNAVAILABLE)
register_error_kind(ValueError, ErrorKind.BAD_INPUT)
register_error_kind(TypeError, ErrorKind.BAD_INPUT)
register_error_kind(PermissionError, ErrorKind.UNAUTHORIZED)
register_error_kind(NotImplementedError, ErrorKind.BAD_INPUT)


class ClientDisconnectedError(Exception):
    """Raised when a client disconnects unexpectedly and is removed from the cache.

    This is not a permanent failure — retrying the operation will cause a fresh
    client to be created automatically.
    """

    # A dropped pooled client is a transient reach-the-dependency failure.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class ClientConnectError(ClientDisconnectedError):
    """Raised when a pooled client could not be (re)built — connect/init failed; a
    subclass so unavailable-client handling catches both."""

    # A failed (re)connect is likewise a transient dependency-unavailable failure.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


__all__ = [
    "ERROR_KIND_ATTR",
    "ClientConnectError",
    "ClientDisconnectedError",
    "ErrorKind",
    "error_kind",
    "register_error_kind",
]
