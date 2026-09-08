"""The base content-store contract.

``Storage`` is "where content is stored" — distinct from
:mod:`tai42_contract.template` ("what we do with it", the render mixins). The store
exposes the text surface ``load`` / ``list`` / ``upload`` / ``delete`` /
``delete_dir`` plus the binary/media surface ``load_bytes`` / ``upload_bytes`` /
``stat``, with the module-level ``assert_not_root`` guarding deletes against the
store root. The binary methods ship text-bridging defaults so a text-only backend
satisfies the whole contract with no new code; binary-native backends override.
"""

from __future__ import annotations

import builtins
import mimetypes
import posixpath
from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from tai42_contract.errors import ErrorKind

# Cap on how many conflicting ids a human message spells out; the full set stays
# on ``conflicts`` so a caller never loses ids to the summary.
_MESSAGE_CONFLICT_CAP = 20


class StoragePathConflictError(Exception):
    """An upload id collides with the store's existing shape.

    A single id space is shared by files and directories, so one id can name only
    one of them: an id that still names a directory holding objects cannot also
    become a file, and an object cannot be stored beneath an id already held as a
    file. A backend raises this instead of leaking a backend-specific error, so a
    surface maps it to a 409 conflict. ``path`` is the rejected id; ``conflicts``
    names the stored ids it collides with.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, path: str, conflicts: list[str]) -> None:
        self.path = path
        self.conflicts = list(conflicts)
        super().__init__(f"storage path {path!r} collides with existing objects: {self.conflicts_summary()}")

    def conflicts_summary(self) -> str:
        """The conflicting ids as a human string, capped at
        :data:`_MESSAGE_CONFLICT_CAP` with a ``… and N more`` tail; the full list
        stays on :attr:`conflicts`."""
        ids = self.conflicts
        if not ids:
            return "(none listed)"
        if len(ids) > _MESSAGE_CONFLICT_CAP:
            shown = ", ".join(repr(c) for c in ids[:_MESSAGE_CONFLICT_CAP])
            return f"{shown}, … and {len(ids) - _MESSAGE_CONFLICT_CAP} more"
        return ", ".join(repr(c) for c in ids)


class ObjectStat(BaseModel):
    """Metadata for a stored object, returned by :meth:`Storage.stat`.

    Carries only ``content_type`` — the MIME type consumed here for image/audio
    media gating (a backend that stores no metadata infers it from the path, a
    metadata-bearing backend returns the stored type). Frozen: a stat snapshot is
    a value, not a mutable handle.
    """

    model_config = ConfigDict(frozen=True)

    content_type: str | None


def assert_not_root(path: str) -> None:
    """Reject a directory path that resolves to the storage root.

    A blank path, one consisting only of slashes and dots (``""``, ``"."``,
    ``"/"``, ``"  "``), or one whose normalized form escapes to or above the
    root (``"a/.."``, ``"./x/../.."``) would target every stored item, so
    deleting it is refused. Shared by all storage backends to keep the guard
    identical.
    """
    if not path or path.strip().strip("/.") == "":
        raise ValueError("Refusing to delete the storage root; a directory path is required.")
    normalized = posixpath.normpath(path.strip())
    if normalized in (".", "/", "//") or normalized == ".." or normalized.startswith(("../", "/../")):
        raise ValueError("Refusing to delete the storage root; a directory path is required.")


class Storage(ABC):
    @abstractmethod
    async def load(self, path: str) -> str:
        """Return the content at ``path``.

        Must raise ``FileNotFoundError`` when the item does not exist — the
        manager relies on that exact type to map a missing ``{% include %}`` /
        ``{% extends %}`` dependency to Jinja's ``TemplateNotFound`` (so
        ``{% include ... ignore missing %}`` works). Any other failure (auth,
        network) must propagate as its own error.
        """
        raise NotImplementedError

    @abstractmethod
    async def list(self) -> builtins.list[str]:
        raise NotImplementedError

    @abstractmethod
    async def upload(self, path: str, content: str) -> None:
        """Store ``content`` at ``path`` (create or overwrite).

        Files and directories share one id space, so an upload whose ``path``
        collides with the store's existing shape — an id that still names a
        non-empty directory, or an id nested beneath an id already held as a file
        — raises :class:`StoragePathConflictError`. An id naming an empty leftover
        directory is free to hold a file.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Delete the object at ``path``, raising ``FileNotFoundError`` when it
        does not exist (a caller wanting idempotent semantics maps that to a
        no-op)."""
        raise NotImplementedError

    @abstractmethod
    async def delete_dir(self, path: str) -> None:
        """Delete every object under ``path``. Path and existence validation
        (``ValueError`` / ``FileNotFoundError``) must complete before any
        deletion begins — once destruction starts, failures surface as other
        exception types, so callers can treat those two as pre-mutation."""
        raise NotImplementedError

    async def load_bytes(self, path: str) -> bytes:
        """Return the raw bytes at ``path``, with the same ``FileNotFoundError``
        contract as :meth:`load`.

        Text-bridge default: reads the text via :meth:`load` and UTF-8 encodes it,
        so a text-only backend serves bytes for free. A binary-native backend
        (e.g. S3) overrides to return the stored bytes unaltered.
        """
        return (await self.load(path)).encode("utf-8")

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Store raw ``data`` at ``path``, optionally tagging ``content_type``.

        Text-bridge default: STRICT-decodes ``data`` as UTF-8 and stores it via
        :meth:`upload`; ``content_type`` is ignored (a text backend keeps no MIME
        metadata). The decode never passes ``errors=`` — non-UTF-8 bytes raise
        ``UnicodeDecodeError`` loudly rather than corrupting the stored content. A
        binary-native backend overrides to store ``data`` and ``content_type`` as-is.
        """
        await self.upload(path, data.decode("utf-8"))

    async def stat(self, path: str) -> ObjectStat:
        """Return metadata for the object at ``path``.

        Path-inference default: guesses ``content_type`` from the path suffix via
        ``mimetypes.guess_type`` — it does NOT verify existence (it answers from
        the path string), so a standalone ``stat`` on a metadata-less backend is
        best-effort metadata; existence for a real read is enforced by the paired
        :meth:`load_bytes`. A metadata-bearing backend (e.g. S3) overrides to
        return the stored content-type and maps a missing object to
        ``FileNotFoundError``.
        """
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type)


__all__ = ["ObjectStat", "Storage", "StoragePathConflictError", "assert_not_root"]
