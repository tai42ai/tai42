"""Response models for the storage operations: stored entries, storage info and resource reads and writes.

A stored entry is discriminated on ``kind`` (text, document or binary); the templates
operations extend these entries with their template-specific fields.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel


class StoredTextEntry(BaseModel):
    """A stored object the classifier reads as text, with its UTF-8 content."""

    kind: Literal["text"]
    id: str
    content_type: str | None
    size: int
    content: str


class StoredDocumentEntry(BaseModel):
    """A stored document (PDF, DOCX, …): ``content`` is the text extracted from it, not its stored bytes."""

    kind: Literal["document"]
    id: str
    content_type: str | None
    size: int
    content: str


class StoredBinaryEntry(BaseModel):
    """A stored binary object: its metadata, with no content."""

    kind: Literal["binary"]
    id: str
    content_type: str | None
    size: int


StoredEntryView = Annotated[StoredTextEntry | StoredDocumentEntry | StoredBinaryEntry, Field(discriminator="kind")]


class StoredEntry(RootModel[StoredEntryView]):
    """One stored object as a text, document or binary entry."""


class StorageInfo(BaseModel):
    """The storage provider identity, or ``present: false`` when none is installed.

    ``surfaces`` names the management routers this deployment mounts over the store.
    """

    present: bool
    provider: str | None
    module: str | None
    surfaces: list[Literal["storage", "templates"]]


class ResourceList(BaseModel):
    """The sorted storage resource ids under a keyed envelope (the wire shape as-is)."""

    resources: list[str]


class ResourceStat(BaseModel):
    """One storage resource's inferred content type; ``null`` when the provider cannot infer one."""

    id: str
    content_type: str | None


class ResourceStored(BaseModel):
    """A storage-resource upload confirmation."""

    id: str
    stored: bool


class ResourceDeleted(BaseModel):
    """A storage-resource delete confirmation."""

    id: str
    deleted: bool


class DirDeleted(BaseModel):
    """A storage-directory delete confirmation."""

    dir: str
    deleted: bool
