"""Shared exception types and the stable failure taxonomy for the TAI ecosystem."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


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

ERROR_TRANSPORT_ATTR = "__tai_transport_error__"
"""The class or instance attribute that marks an exception as a failure of a platform-owned transport."""

# The fallback registry: maps an exception CLASS to its kind (and whether it is a
# platform transport failure) for third-party classes that cannot be stamped at
# their definition site. Seeded below with the stdlib builtins; :func:`error_kind`
# and :func:`is_transport_error` walk an exception's MRO over it so the
# MOST-DERIVED registered ancestor wins.
_ERROR_KIND_REGISTRY: dict[type[BaseException], tuple[ErrorKind, bool]] = {}

# Bound on the ``__cause__`` chain :func:`error_kind` follows before giving up — a
# typed cause five links deep is still recovered, a pathological/cyclic chain never
# spins.
_MAX_CAUSE_DEPTH = 5


def register_error_kind(exc_type: type[BaseException], kind: ErrorKind, *, transport: bool = False) -> None:
    """Register ``kind`` (and whether ``exc_type`` is a platform transport failure) as the fallback for ``exc_type``.

    For a THIRD-PARTY exception class that cannot carry an :data:`ERROR_KIND_ATTR`
    stamp at its definition site (a vendor SDK's error, say). A stamp on the class
    or instance always wins over the registry; among registrations the most-derived
    ancestor wins. Registering an already-registered class overwrites it.
    ``transport=True`` is declared by the owner of the transport the class belongs to.
    """
    _ERROR_KIND_REGISTRY[exc_type] = (ErrorKind(kind), transport)


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


def _registration(exc: BaseException) -> tuple[ErrorKind, bool] | None:
    """The registration of the most-derived registered ancestor of ``type(exc)``."""
    for base in type(exc).__mro__:
        registered = _ERROR_KIND_REGISTRY.get(base)
        if registered is not None:
            return registered
    return None


def _registered_kind(exc: BaseException) -> ErrorKind | None:
    """The kind for the most-derived registered ancestor of ``type(exc)``."""
    registered = _registration(exc)
    return registered[0] if registered is not None else None


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


def is_transport_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a failure of a transport the platform owns, as that transport's owner declared it.

    Read from ``exc`` itself, first hit wins: an :data:`ERROR_TRANSPORT_ATTR` marker on the
    instance or class (a subclass inherits it); else ``False`` when ``exc`` carries an
    :data:`ERROR_KIND_ATTR` stamp (its owner declared its kind and no marker); else the
    transport flag of the most-derived registered ancestor; else ``False``. No ``__cause__``
    is followed: a transport failure caught and re-raised as another error is the
    re-raiser's own failure. A marker that is not a ``bool`` raises ``TypeError``.
    """
    marker = getattr(exc, ERROR_TRANSPORT_ATTR, None)
    if marker is not None:
        if not isinstance(marker, bool):
            raise TypeError(f"{ERROR_TRANSPORT_ATTR} on {type(exc).__name__} must be a bool, got {marker!r}")
        return marker
    if getattr(exc, ERROR_KIND_ATTR, None) is not None:
        return False
    registered = _registration(exc)
    return registered[1] if registered is not None else False


# Seed the registry with the stdlib BUILTINS only — the ecosystem's own errors are
# stamped at their definition sites; these cover the standard exceptions a layer may
# let escape unstamped. A builtin timeout or connection failure is a transport failure.
register_error_kind(TimeoutError, ErrorKind.TIMED_OUT, transport=True)
register_error_kind(ConnectionError, ErrorKind.UNAVAILABLE, transport=True)
register_error_kind(ValueError, ErrorKind.BAD_INPUT)
register_error_kind(TypeError, ErrorKind.BAD_INPUT)
register_error_kind(PermissionError, ErrorKind.UNAUTHORIZED)
register_error_kind(NotImplementedError, ErrorKind.UNAVAILABLE)


class ClientDisconnectedError(Exception):
    """Raised when a client disconnects unexpectedly and is removed from the cache.

    This is not a permanent failure — retrying the operation will cause a fresh
    client to be created automatically.
    """

    # A dropped pooled client is a transient reach-the-dependency failure of a platform transport.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE
    __tai_transport_error__ = True


class ClientConnectError(ClientDisconnectedError):
    """Raised when a pooled client could not be (re)built — connect/init failed.

    A subclass so unavailable-client handling catches both.
    """

    # A failed (re)connect is likewise a transient dependency-unavailable failure.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class ServerReloadingError(Exception):
    """The server is applying a config reload and refuses new work; retry shortly.

    Neither a ``ValueError`` nor a ``LookupError``: a caller that acknowledges and drops bad
    input must not swallow a refusal the sender is meant to retry.
    """

    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class OperationError(Exception):
    """Base class for every declared operation failure.

    ``status`` is the HTTP status the route adapter answers with and the family
    the projection reports; subclasses set it. The base itself carries ``500`` so
    an operation raising the base (rather than a specific subclass) still maps to
    a defined status rather than an unhandled crash.

    ``__tai_error_kind__`` is the transport-neutral twin of ``status``: the same
    failure named for a non-HTTP edge (an agent, the CLI, a projected tool).
    Subclasses set it beside their ``status``; the base's ``500`` maps to an
    upstream/internal fault.
    """

    status: ClassVar[int] = 500
    # A bare operation failure (base 500) is an upstream/internal fault.
    __tai_error_kind__: ClassVar[ErrorKind] = ErrorKind.UPSTREAM_ERROR

    def __init__(self, message: str, *, extra: dict[str, object] | None = None) -> None:
        """Build the error with ``message`` and optional ``extra`` fields merged into the error body."""
        super().__init__(message)
        self.message = message
        # Additional fields merged into the ``{"error": …}`` body the adapter emits
        # (e.g. a stable ``code`` a UI keys a dedicated error state on). Empty by
        # default, so an error carries only ``{"error": …}`` unless a raiser opts in.
        self.extra: dict[str, object] = extra or {}


class UnavailableError(OperationError):
    """A dependency the operation needs is temporarily unavailable."""

    status: ClassVar[int] = 503
    # A temporarily unavailable dependency.
    __tai_error_kind__: ClassVar[ErrorKind] = ErrorKind.UNAVAILABLE


__all__ = [
    "ERROR_KIND_ATTR",
    "ERROR_TRANSPORT_ATTR",
    "ClientConnectError",
    "ClientDisconnectedError",
    "ErrorKind",
    "OperationError",
    "ServerReloadingError",
    "UnavailableError",
    "error_kind",
    "is_transport_error",
    "register_error_kind",
]
