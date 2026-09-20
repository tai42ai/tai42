"""Record models for the generic versioned-document store.

The store is a first-class persistence primitive: append-only versions over an
opaque JSONB ``body``, plus an active-version pointer, discriminated by ``kind``.
It knows nothing about what any ``kind`` stores — presets, AC policies, authored
agents are all typed VIEWS on top. Identity is ``(kind, name)`` throughout.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentRecord(BaseModel):
    """A versioned document's live record: its identity plus the active pointer.

    ``active_version`` names the version currently served for ``(kind, name)``;
    rollback re-points it without copying data. ``is_active`` is the soft-delete
    flag — a soft-deleted document keeps its version history (audit) but drops out
    of ``list``/``get``. ``created_at`` is an ISO-8601 timestamp string.
    """

    kind: str
    name: str
    active_version: int
    is_active: bool = True
    created_at: str


class DocumentVersion(BaseModel):
    """One immutable version row: the opaque ``body`` plus its version number.

    A version is append-only — once written, its ``version``, ``body``, ``tags``,
    and ``created_at`` never change. ``tags`` is a generic per-version label for
    grouping versions (release/grouping labels); it is kind-agnostic and carries
    no product meaning (distinct from any categorization a ``kind``'s body may
    hold). ``is_current`` is a read-time projection of the owning record's
    ``active_version`` — the store sets it ``True`` on the active version when it
    lists/returns versions; it is not stored on the row. ``created_at`` is an
    ISO-8601 timestamp string.
    """

    model_config = ConfigDict(frozen=True)

    version: int
    body: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    created_at: str
    is_current: bool = False
