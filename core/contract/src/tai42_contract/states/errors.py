"""Errors the subject-keyed state store raises.

Each stamps its transport-neutral :class:`~tai42_contract.errors.ErrorKind`; the
door status the operations layer surfaces is noted per class (501 not configured,
404 not found, 409 exists / in use / conflict / a failed pending save, 422 schema /
subject / regime / path / value / a refused deferred call, 412 narrowing needs
confirmation, 503 a pending save that did not finish in time). Every path raises
loudly — the store has no silent no-op.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.errors import ErrorKind


class StatesError(Exception):
    """Base class for every state-store error.

    ``extra`` is an OPTIONAL structured payload a raiser attaches for the door to surface
    on the error body beside the message (e.g. the orphan list of a reconcile refusal), so
    a UI keys its follow-up on the data, not on the prose. The message stays authoritative;
    ``extra`` never replaces it.
    """

    # A bare store fault carries no more specific classification.
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, *args: object, extra: dict[str, Any] | None = None) -> None:
        """Build the error with the usual message ``args`` plus an optional ``extra`` payload for the door."""
        super().__init__(*args)
        self.extra: dict[str, Any] | None = extra


class StatesNotConfiguredError(StatesError):
    """The ``states`` component's database is unbound (501 ``states-not-configured``).

    Every read and write refuses rather than serving an empty document.
    """

    # A capability this deployment cannot serve until its database is bound.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class StateNotFoundError(StatesError):
    """No declaration exists for the named state (404)."""

    __tai_error_kind__ = ErrorKind.NOT_FOUND


class StateExistsError(StatesError):
    """A declaration named an existing state without ``replace`` (409).

    The existing name rides on the message so the caller can offer a re-declare.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class SubjectRefusedError(StatesError):
    """A subject was refused (422).

    The message names which rule fired: an undeclared kind, an empty key, an
    unknown person, or a person whose target does not match the subject's.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class SchemaValidationError(StatesError):
    """A declaration schema is not a valid object-rooted JSON Schema (422)."""

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class NonAdditiveRedeclareError(StatesError):
    """A plain re-declare would remove or change a field while records exist (409).

    The records must be erased first, never a silent re-declare.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class DeclarationInUseError(StatesError):
    """A declaration delete or subject-kind removal was refused because records still reference it (409)."""

    __tai_error_kind__ = ErrorKind.CONFLICT


class InvalidPathError(StatesError):
    """An op path is malformed or structurally impossible against the document (422)."""

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class ValueValidationError(StatesError):
    """The document, after applying the ops, does not validate against the effective schema (422).

    The message names the offending JSON path.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class RegimeViolationError(StatesError):
    """A write's SHAPE violates an attached path's regime (422).

    A whole-path ``set``/``remove`` over a ``composing`` path. The message names the path.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class SubjectFoldError(StatesError):
    """A subject fold was refused (409).

    A self-fold, a cycle, a conflicting re-fold, or a merge whose combined
    document does not validate. The whole fold aborts.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class TemplateValidationError(StatesError):
    """A state-template document, or an attachment's declaration values against it, violates a structural rule (422).

    The message names the offending rule and path.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class TemplateExistsError(StatesError):
    """A template upload named an existing template without ``replace`` (409).

    The existing name rides on the message so the caller can offer an overwrite.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class TemplateInUseError(StatesError):
    """A template delete or replace was refused (409).

    A delete because the template is still attached, or a replace that no longer
    validates against one of its live attachments.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class AttachConflictError(StatesError):
    """Composing the effective schema hit an overlapping path (409).

    Two attachment fragments landed at overlapping paths, or an attachment
    landed where the base schema already carries the property. The message names
    the colliding path.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class StatePendingSaveFailedError(StatesError):
    """A subject's pending save failed; the subject is held until an operator retries or discards it (409).

    ``save_id`` names the FAILED save (for a save held behind it, the failed one it waits on) and
    rides on ``extra``.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, *args: object, save_id: str) -> None:
        """Build the error naming the failed ``save_id``."""
        super().__init__(*args, extra={"save_id": save_id})
        self.save_id = save_id


class StatePendingSaveTimeoutError(StatesError):
    """A subject's pending save did not finish within the drain timeout (503).

    ``save_id`` names the save still working and rides on ``extra``.
    """

    __tai_error_kind__ = ErrorKind.UNAVAILABLE

    def __init__(self, *args: object, save_id: str) -> None:
        """Build the error naming the still-working ``save_id``."""
        super().__init__(*args, extra={"save_id": save_id})
        self.save_id = save_id


class DeferredCallRefusedError(StatesError):
    """A call deferred after the reply was refused (422).

    The tool is unknown, its arguments are not plain JSON, or the call parked for an answer.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT
