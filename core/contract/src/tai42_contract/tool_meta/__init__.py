"""The tool-metadata contract: the folder + overlay record models, the
tool-metadata errors, and the :class:`ToolMetaStore` Protocol.

tool_meta is an UNVERSIONED organizational overlay over ANY tool in the
namespace, keyed by tool name — folders (real nesting entities) plus a per-tool
row of ``display_name`` / ``folder_id`` / ``tags`` / ``hidden`` / ``badges``. tai42-contract
owns only the Protocol + models + errors; the concrete store (plain Postgres
tables, cycle prevention, clean-slate name reclaim) lives in the skeleton, the
same models-only split the preset contract follows.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tai42_contract.tool_meta.errors import (
    FolderCycleError,
    FolderNameConflictError,
    FolderNotEmptyError,
    FolderNotFoundError,
    ToolMetaError,
)
from tai42_contract.tool_meta.models import FolderRecord, ToolMetaRecord


@runtime_checkable
class ToolMetaStore(Protocol):
    """The typed interface over the tool-metadata tables.

    Folder mutations enforce sibling-name uniqueness and cycle-freedom, and a
    delete requires the folder empty of subfolders AND tool-meta rows. Overlay
    writes are keyed by tool name: ``upsert_meta`` persists a FULL resolved row,
    while ``merge_meta`` resolves a merge-patch against the current row ATOMICALLY
    in one transaction (the read-modify-write the operation layer must never split
    across two connections, else concurrent patches lose an update).
    ``delete_meta`` and ``rename_tool`` are NO-OPS when the tool has no overlay row
    — they run on every preset delete/rename and most tools never own a row. Every
    other failure raises loudly.
    """

    async def create_folder(self, name: str, parent_id: str | None = None) -> FolderRecord:
        """Create a folder under ``parent_id`` (``None`` = root). Raise
        :class:`FolderNotFoundError` for an unknown ``parent_id`` and
        :class:`FolderNameConflictError` when a sibling already holds ``name``."""
        ...

    async def rename_folder(self, folder_id: str, name: str) -> FolderRecord:
        """Rename a folder in place. Raise :class:`FolderNotFoundError` if absent
        and :class:`FolderNameConflictError` on a sibling collision."""
        ...

    async def move_folder(self, folder_id: str, parent_id: str | None) -> FolderRecord:
        """Re-parent a folder (``None`` = root). Raise :class:`FolderNotFoundError`
        if the folder or ``parent_id`` is absent, :class:`FolderCycleError` if the
        move would make the folder its own ancestor, and
        :class:`FolderNameConflictError` on a sibling collision at the destination."""
        ...

    async def delete_folder(self, folder_id: str) -> None:
        """Delete an EMPTY folder. Raise :class:`FolderNotFoundError` if absent and
        :class:`FolderNotEmptyError` if it still holds subfolders or tool-meta rows."""
        ...

    async def list_folders(self) -> list[FolderRecord]:
        """List every folder (the whole tree, flat)."""
        ...

    async def upsert_meta(
        self,
        tool_name: str,
        *,
        display_name: str | None,
        folder_id: str | None,
        tags: list[str],
        hidden: bool | None,
        badges: list[str] | None = None,
    ) -> ToolMetaRecord:
        """Write the FULL overlay row for ``tool_name`` (insert or replace). The
        caller passes the already-resolved state — merge-patch against the current
        row is resolved a layer up. ``badges`` defaults to ``None``, written as the
        empty set. Raise :class:`FolderNotFoundError` when ``folder_id`` names no
        folder."""
        ...

    async def merge_meta(self, tool_name: str, *, patch: dict[str, Any]) -> ToolMetaRecord:
        """Atomically merge-patch (RFC-7396) the overlay row for ``tool_name`` in a
        SINGLE transaction: read the current row under a row lock, apply only the
        fields PRESENT in ``patch`` over it (creating the row from empty defaults
        when absent), and persist the result — so two concurrent patches to the
        same tool serialize on the lock instead of racing on separate connections
        and losing an update. ``patch`` carries ONLY the keys the caller sent
        (``display_name`` / ``folder_id`` / ``tags`` / ``hidden`` / ``badges``); a
        present value writes — including a present ``None`` that CLEARS — while
        ``tags`` and ``badges`` each replace the whole set. ``display_name`` is already normalized by the caller (the
        blank-display-name refusal is the operation layer's job). Raise
        :class:`FolderNotFoundError` when a present ``folder_id`` names no folder."""
        ...

    async def get_meta(self, tool_name: str) -> ToolMetaRecord | None:
        """Return the overlay row for ``tool_name``, or ``None`` when the tool has
        no row (the common case — most tools never get one)."""
        ...

    async def list_meta(self) -> list[ToolMetaRecord]:
        """List every overlay row."""
        ...

    async def delete_meta(self, tool_name: str) -> None:
        """Delete the overlay row for ``tool_name``. NO-OP when no row exists."""
        ...

    async def rename_tool(self, old_name: str, new_name: str) -> None:
        """Re-key the overlay row from ``old_name`` to ``new_name``, atomically
        deleting any pre-existing ``new_name`` row first (clean slate — a freed
        name never inherits a ghost's overlay). When ``old_name`` owns no row the
        MOVE is a no-op, but a pre-existing ``new_name`` ghost is still cleared so
        the renamed tool keeps its no-overlay state."""
        ...


__all__ = [
    "FolderCycleError",
    "FolderNameConflictError",
    "FolderNotEmptyError",
    "FolderNotFoundError",
    "FolderRecord",
    "ToolMetaError",
    "ToolMetaRecord",
    "ToolMetaStore",
]
