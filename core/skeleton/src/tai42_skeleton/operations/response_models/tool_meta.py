"""Response models for the tool-meta operations: tool-meta records and folders."""

from __future__ import annotations

from pydantic import BaseModel
from tai42_contract.tool_meta import FolderRecord, ToolMetaRecord


class ToolMetaListView(BaseModel):
    """The whole tool-metadata overlay in one read: the flat folder tree plus every per-tool row."""

    folders: list[FolderRecord]
    meta: list[ToolMetaRecord]


class ToolMetaDeleted(BaseModel):
    """A tool-overlay-row delete confirmation."""

    tool_name: str
    deleted: bool


class FolderDeleted(BaseModel):
    """A folder delete confirmation."""

    folder_id: str
    deleted: bool
