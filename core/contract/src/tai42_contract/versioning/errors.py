"""Errors raised by the generic versioned-document store.

Every method raises loudly on a missing/duplicate/invalid target — never a silent
default. Each error carries the ``(kind, name)`` identity of the document it
concerns so a caller (or a typed view mapping to its own error type) can report
the offending document precisely.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class DocumentStoreError(Exception):
    """Base for versioned-document store failures, carrying ``(kind, name)``."""

    # A bare store failure is an unclassified fault of the persistence layer the
    # caller delegates to (concrete subclasses stamp their own specific kind).
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, kind: str, name: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.name = name


class DocumentExistsError(DocumentStoreError):
    """A live document already exists for ``(kind, name)`` (create would collide)."""

    # A create colliding with a live document.
    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, kind: str, name: str):
        super().__init__(kind, name, f"document {name!r} of kind {kind!r} already exists")


class DocumentNotFoundError(DocumentStoreError):
    """No active document for ``(kind, name)``."""

    # The addressed document does not exist.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, kind: str, name: str):
        super().__init__(kind, name, f"no active document {name!r} of kind {kind!r}")


class DocumentVersionNotFoundError(DocumentStoreError):
    """No version ``version`` exists for the ``(kind, name)`` document.

    ``version`` is carried when known (the version-specific ops always know it);
    the ``(kind, name)`` identity is always present, as for the other store errors.
    """

    # A requested version of the document does not exist — a not-found target.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, kind: str, name: str, version: int | None = None):
        self.version = version
        detail = "" if version is None else f" version {version}"
        super().__init__(kind, name, f"no{detail} version for document {name!r} of kind {kind!r}")
