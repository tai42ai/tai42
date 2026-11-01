"""The marketplace failure family translated to the shared operation-error vocabulary.

The internal :class:`~tai42_skeleton.marketplace.errors.MarketplaceError` family is
load-bearing inside the marketplace package (it carries the not-installed flag,
the pip argv/return code, the two errors of a failed unwind). At the operations
boundary each is translated by :func:`to_operation_error`, its message the error's text
followed by its notes (:func:`~tai42_skeleton.operations.errors.operator_message`; a
failed operation whose unwind restored the previous plugin environment says so on the
last line): a dead or garbled registry is an :class:`UpstreamError` (502 — the
marketplace surface proxies the registry, so an upstream failure is never a this-server
500); an in-progress operation elsewhere in the fleet is an :class:`UnavailableError`
(503, retriable); an unknown listing or a not-installed ref is a :class:`NotFoundError`
(404); a version/collision/contract, already-installed, or plugin-dependency conflict,
an ``mcp-server`` command its package does not declare as a console script, or a console
script another installed server package already exposes, is a :class:`ConflictError`
(409); a pip failure, a github artifact-integrity mismatch, a failed unwind, a plugin
migration the runner refused, a manifest-compose fault, an unknown-item-kind binding
drift, or corrupt local state is an :class:`OperationFailedError` (500). A malformed
``ref`` is the caller's own author error — a typed
:class:`~tai42_skeleton.marketplace.errors.MalformedRefError` the boundary maps to
:class:`BadRequestError` (400).

Retry-After limitation: the 503 an in-progress operation answers carries no
``Retry-After`` header. The route adapter stamps static headers on the SUCCESS
response only; an error response is header-less by construction, and the typed
error's ``extra`` dict merges into the JSON body, not the headers. The retriable
signal therefore rides the message and the 503 status, not a header. (The
reload-gate 503 — a separate concern the adapter honors from ``reload_gated``
metadata — DOES carry ``Retry-After: 5``, since that response is built by the
reload gate itself, not the error path.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast

from tai42_contract.errors import OperationError, UnavailableError

from tai42_skeleton.marketplace.errors import (
    ArtifactIntegrityError,
    ContractIncompatibleError,
    InstallEnvError,
    InstallStateError,
    ListingNotFoundError,
    MalformedRefError,
    ManifestCollisionError,
    ManifestRefusedError,
    MarketplaceError,
    OperationInProgressError,
    PipFailedError,
    PluginDependencyConflictError,
    PublicRoutesNotAcceptedError,
    RegistryResponseError,
    RegistryUnreachableError,
    ReservedRoutePrefixError,
    RouteCollisionError,
    RouteMountError,
    ServerCommandCollisionError,
    UndeclaredServerCommandError,
    VersionRefusedError,
)
from tai42_skeleton.operations.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    OperationFailedError,
    UpstreamError,
    operator_message,
)

logger = logging.getLogger(__name__)

# Keep the tail of an oversized detail (a pip failure's captured output) for the
# error envelope; the full text is logged whole. The prefix marks a cut.
_ENVELOPE_DETAIL_CHARS = 4000


def _truncate(text: str) -> str:
    """The last ``_ENVELOPE_DETAIL_CHARS`` of ``text``, with a visible cut marker when text was dropped."""
    if len(text) <= _ENVELOPE_DETAIL_CHARS:
        return text
    return f"... (truncated) {text[-_ENVELOPE_DETAIL_CHARS:]}"


def _pip_failed_error(exc: MarketplaceError) -> OperationError:
    """A pip failure → 500.

    The full captured output stays in the log; the envelope carries the summary plus
    a truncated tail so the operator sees the failing lines.
    """
    pip = cast("PipFailedError", exc)
    logger.error("marketplace pip failure: %s\n%s", pip, pip.output)
    return OperationFailedError(operator_message(pip), extra={"pip_output": _truncate(pip.output)})


def _artifact_integrity_error(exc: MarketplaceError) -> OperationError:
    """A github artifact whose sha256 disagrees with the registry's ingest digest.

    An install-integrity failure (a possibly re-pointed release tag), a loud terminal
    500 carrying the digests and the rejected URL.
    """
    art = cast("ArtifactIntegrityError", exc)
    logger.error("marketplace artifact integrity failure: %s", art)
    return OperationFailedError(
        operator_message(art),
        extra={
            "artifact_ref": art.artifact_ref,
            "expected_sha256": art.expected_sha256,
            "actual_sha256": art.actual_sha256,
        },
    )


def _public_routes_error(exc: MarketplaceError) -> OperationError:
    """Declared public routes need the operator's explicit acceptance — a 400.

    Carries a stable code + the rows so the UI can render the acceptance gate.
    """
    err = cast("PublicRoutesNotAcceptedError", exc)
    return BadRequestError(
        operator_message(err), extra={"code": "PUBLIC_ROUTES_NOT_ACCEPTED", "public_routes": err.public_routes}
    )


def _route_collision_error(exc: MarketplaceError) -> OperationError:
    """A declared route clashes with an already-owned one — a 409 carrying the collision list.

    The remedy is to remap the item's base.
    """
    err = cast("RouteCollisionError", exc)
    return ConflictError(operator_message(err), extra={"code": "ROUTE_COLLISION", "collisions": err.collisions})


def _reserved_prefix_error(exc: MarketplaceError) -> OperationError:
    """A declared public route resolves under a reserved never-public prefix — a 409.

    Carries the offending paths; the remedy is to remap the base.
    """
    err = cast("ReservedRoutePrefixError", exc)
    return ConflictError(operator_message(err), extra={"code": "ROUTE_RESERVED_PREFIX", "routes": err.offenders})


def _install_state_error(exc: MarketplaceError) -> OperationError:
    """A not-installed ref is a 404; every other state conflict is a 409."""
    err = cast("InstallStateError", exc)
    return NotFoundError(operator_message(err)) if err.not_installed else ConflictError(operator_message(err))


# Ordered (predicate, builder) pairs in classification precedence order; the FIRST
# matching predicate wins, so the classification keys on the typed class (and, for a
# state conflict, its ``not_installed`` flag) — never on message text. A failure matching
# none is the terminal ``OperationFailedError`` below.
_ERROR_RULES: tuple[tuple[Callable[[MarketplaceError], bool], Callable[[MarketplaceError], OperationError]], ...] = (
    # The caller's own author error — a malformed ref, an mcp-server install missing a
    # required !ENV value, a composed manifest the config pipeline refuses, or a
    # route_mounts override naming a non-route item / bad base.
    (
        lambda e: isinstance(e, MalformedRefError | InstallEnvError | ManifestRefusedError | RouteMountError),
        lambda e: BadRequestError(operator_message(e)),
    ),
    (lambda e: isinstance(e, PublicRoutesNotAcceptedError), _public_routes_error),
    (lambda e: isinstance(e, RouteCollisionError), _route_collision_error),
    (lambda e: isinstance(e, ReservedRoutePrefixError), _reserved_prefix_error),
    # The registry is this surface's upstream; a dead/garbled upstream is a 502, never a
    # this-server 500 and never a promise that retry fixes it.
    (
        lambda e: isinstance(e, RegistryUnreachableError | RegistryResponseError),
        lambda e: UpstreamError(operator_message(e)),
    ),
    (lambda e: isinstance(e, ListingNotFoundError), lambda e: NotFoundError(operator_message(e))),
    # Another worker (or this one) holds the fleet-wide marketplace lock — retriable. See
    # the module docstring's Retry-After limitation.
    (lambda e: isinstance(e, OperationInProgressError), lambda e: UnavailableError(operator_message(e))),
    (lambda e: isinstance(e, InstallStateError), _install_state_error),
    # State conflicts the operator resolves, not by retrying as-is (a plugin whose
    # dependencies conflict with the installed plugins or the image, whose mcp-server
    # command its package does not declare, or whose server package declares a console
    # script another installed server package exposes, is re-pinned, the other plugin
    # uninstalled, or the image rebuilt).
    (
        lambda e: isinstance(
            e,
            VersionRefusedError
            | ManifestCollisionError
            | ContractIncompatibleError
            | UndeclaredServerCommandError
            | ServerCommandCollisionError
            | PluginDependencyConflictError,
        ),
        lambda e: ConflictError(operator_message(e)),
    ),
    (lambda e: isinstance(e, PipFailedError), _pip_failed_error),
    (lambda e: isinstance(e, ArtifactIntegrityError), _artifact_integrity_error),
)


def to_operation_error(exc: MarketplaceError) -> OperationError:
    """Translate an internal marketplace failure to its honest operation error.

    The classification keys on the typed class (and, for a state conflict, its
    ``not_installed`` flag) — never on message text.
    """
    for predicate, build in _ERROR_RULES:
        if predicate(exc):
            return build(exc)
    # PipUnavailableError / InstallUnwindError / PluginMigrationError / ManifestComposeError /
    # LocalStateError / the base: the deployment environment failed the operation.
    return OperationFailedError(_truncate(operator_message(exc)))
