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
        raise NotImplementedError

    @abstractmethod
    async def delete(self, path: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_dir(self, path: str) -> None:
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


__all__ = ["ObjectStat", "Storage", "assert_not_root"]
