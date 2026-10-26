"""The base content-store contract.

``Storage`` is "where content is stored" — distinct from
:mod:`tai42_contract.template` ("what we do with it", the render mixins). The store
exposes the text surface ``load`` / ``list`` / ``upload`` / ``delete`` /
``delete_dir``, the metadata read ``stat`` (content type and byte size), the bounded
read ``load_range``, the binary surface ``load_bytes`` / ``upload_bytes`` and the
whole-object read ``load_object`` (the bytes with their content type), with the
module-level ``assert_not_root`` guarding deletes against the store root. ``stat`` and
``load_range`` are abstract: only the backend knows an object's size and how to read a
slice of it without reading all of it. ``load_bytes`` / ``upload_bytes`` ship
text-bridging defaults and ``load_object`` a default built on ``load_bytes`` and
``stat``; binary-native backends override them.
"""

from __future__ import annotations

import builtins
import posixpath
from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field

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
        """Record the rejected ``path`` and the stored ids it ``conflicts`` with."""
        self.path = path
        self.conflicts = list(conflicts)
        super().__init__(f"storage path {path!r} collides with existing objects: {self.conflicts_summary()}")

    def conflicts_summary(self) -> str:
        """The conflicting ids as a human string, capped at :data:`_MESSAGE_CONFLICT_CAP`.

        Beyond the cap a ``… and N more`` tail is appended; the full list stays on
        :attr:`conflicts`.
        """
        ids = self.conflicts
        if not ids:
            return "(none listed)"
        if len(ids) > _MESSAGE_CONFLICT_CAP:
            shown = ", ".join(repr(c) for c in ids[:_MESSAGE_CONFLICT_CAP])
            return f"{shown}, … and {len(ids) - _MESSAGE_CONFLICT_CAP} more"
        return ", ".join(repr(c) for c in ids)


class ObjectStat(BaseModel):
    """Metadata for a stored object, returned by :meth:`Storage.stat`.

    Carries the content type and the byte size. ``content_type`` is the MIME type
    consumed for media gating (a backend that stores no metadata infers it from the
    path, a metadata-bearing backend returns the stored type); ``size`` is the stored
    object's length in bytes. Frozen: a stat snapshot is a value, not a mutable handle.
    """

    model_config = ConfigDict(frozen=True)

    content_type: str | None
    size: int = Field(ge=0)


class StoredObject(BaseModel):
    """A stored object's bytes and its content type, returned by :meth:`Storage.load_object`.

    ``content_type`` is the same MIME type :meth:`Storage.stat` reports. Frozen: a read
    object is a value, not a mutable handle.
    """

    model_config = ConfigDict(frozen=True)

    data: bytes
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


def directory_prefix(directory: str) -> str:
    """The key prefix every object beneath ``directory`` starts with: the directory and one ``/``.

    Surrounding slashes are dropped (``"a/b/"`` and ``"/a/b"`` are ``"a/b/"``). A blank
    ``directory`` (``""``, ``"/"``) names no directory and raises ``ValueError``.
    """
    name = directory.strip("/")
    if not name.strip():
        raise ValueError("A directory path is required; a blank path names no directory.")
    return f"{name}/"


class Storage(ABC):
    """The abstract object-store contract: load, list, upload and delete objects by path.

    Files and directories share one id space; a backend maps missing objects to
    ``FileNotFoundError`` and colliding paths to :class:`StoragePathConflictError`.
    """

    @abstractmethod
    async def load(self, path: str) -> str:
        """Return the content at ``path``.

        Must raise ``FileNotFoundError`` when the item does not exist — the
        manager relies on that exact type to map a missing ``{% include %}`` /
        ``{% extends %}`` dependency to Jinja's ``TemplateNotFound`` (so
        ``{% include ... ignore missing %}`` works). Content that is not UTF-8
        text must raise ``UnicodeDecodeError`` (a strict decode, never a lossy
        substitution) — the manager maps it to a typed not-a-text-template error.
        Any other failure (auth, network) must propagate as its own error.
        """
        raise NotImplementedError

    @abstractmethod
    async def list(self, directory: str | None = None) -> builtins.list[str]:
        """Return stored object paths, in no guaranteed order.

        With no ``directory``, every object in the store. With a ``directory``, only the
        objects beneath it (the paths starting with :func:`directory_prefix` of it), read
        from the backend by that prefix rather than by listing the whole store; a
        directory holding no objects answers ``[]``. A blank ``directory`` raises
        ``ValueError``.
        """
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
        """Delete the object at ``path``, raising ``FileNotFoundError`` when it does not exist.

        A caller wanting idempotent semantics maps that ``FileNotFoundError`` to a no-op.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete_dir(self, path: str) -> None:
        """Delete every object under ``path``.

        Path and existence validation (``ValueError`` / ``FileNotFoundError``) must complete
        before any deletion begins — once destruction starts, failures surface as other
        exception types, so callers can treat those two as pre-mutation.
        """
        raise NotImplementedError

    async def load_bytes(self, path: str) -> bytes:
        """Return the raw bytes at ``path``, with the same ``FileNotFoundError`` contract as :meth:`load`.

        Text-bridge default: reads the text via :meth:`load` and UTF-8 encodes it,
        so a text-only backend serves bytes for free. A binary-native backend
        (e.g. S3) overrides to return the stored bytes unaltered.
        """
        return (await self.load(path)).encode("utf-8")

    async def load_object(self, path: str) -> StoredObject:
        """Return the bytes and the content type of the object at ``path``.

        Raises ``FileNotFoundError`` when the object does not exist, as :meth:`load` does.

        Default: :meth:`load_bytes` for the bytes and :meth:`stat` for the type, two
        backend reads. A backend that answers both in one read (e.g. S3, whose object
        read carries its stored type) overrides, so a caller wanting both makes one call.
        """
        data = await self.load_bytes(path)
        return StoredObject(data=data, content_type=(await self.stat(path)).content_type)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Store raw ``data`` at ``path``, optionally tagging ``content_type``.

        Text-bridge default: STRICT-decodes ``data`` as UTF-8 and stores it via
        :meth:`upload`; ``content_type`` is ignored (a text backend keeps no MIME
        metadata). The decode never passes ``errors=`` — non-UTF-8 bytes raise
        ``UnicodeDecodeError`` loudly rather than corrupting the stored content. A
        binary-native backend overrides to store ``data`` and ``content_type`` as-is.
        """
        await self.upload(path, data.decode("utf-8"))

    @abstractmethod
    async def stat(self, path: str) -> ObjectStat:
        """Return the content type and the byte size of the object at ``path``.

        Raises ``FileNotFoundError`` when the object does not exist; a directory is
        not an object.
        """
        raise NotImplementedError

    @abstractmethod
    async def load_range(self, path: str, start: int, length: int) -> bytes:
        """Return at most ``length`` bytes of the object at ``path``, from byte ``start``.

        Fewer bytes when the object ends first, ``b""`` when ``start`` is at or past
        the end. ``length == 0`` answers ``b""`` with no request to the backend (and
        so checks no existence). Raises ``FileNotFoundError`` when the object does not
        exist. Never reads the whole object to answer.
        """
        raise NotImplementedError


__all__ = ["ObjectStat", "Storage", "StoragePathConflictError", "StoredObject", "assert_not_root", "directory_prefix"]
