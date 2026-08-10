"""Errors the tool-metadata store raises.

Every failure raises loudly — there is no silent path. Folder errors carry the
``folder_id`` they concern; the name-conflict error carries the conflicting
``name`` (and the ``parent_id`` under which it collides).
"""

from __future__ import annotations


class ToolMetaError(Exception):
    """Base for tool-metadata store failures."""


class FolderNotFoundError(ToolMetaError):
    """No folder with id ``folder_id`` exists."""

    def __init__(self, folder_id: str):
        self.folder_id = folder_id
        super().__init__(f"folder {folder_id!r} not found")


class FolderNotEmptyError(ToolMetaError):
    """A folder delete was refused because the folder still holds subfolders or
    tool-meta rows. A folder is deletable only when empty of both."""

    def __init__(self, folder_id: str):
        self.folder_id = folder_id
        super().__init__(f"folder {folder_id!r} is not empty")


class FolderCycleError(ToolMetaError):
    """A folder MOVE was refused because it would make ``folder_id`` its own
    ancestor (a cycle in the parent chain). This is the move-time guard only:
    creation just validates that the parent exists — a freshly created folder's id
    cannot already sit in any parent chain, so a create can never form a cycle."""

    def __init__(self, folder_id: str):
        self.folder_id = folder_id
        super().__init__(f"moving folder {folder_id!r} there would create a cycle")


class FolderNameConflictError(ToolMetaError):
    """A folder ``name`` already exists under the same ``parent_id`` (``None`` for
    root folders). Sibling names are unique within a parent."""

    def __init__(self, name: str, parent_id: str | None):
        self.name = name
        self.parent_id = parent_id
        where = "at the root" if parent_id is None else f"under folder {parent_id!r}"
        super().__init__(f"a folder named {name!r} already exists {where}")
