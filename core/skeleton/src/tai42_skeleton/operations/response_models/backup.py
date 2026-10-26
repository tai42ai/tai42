"""Response models for the backup operations: the section listing and the import result."""

from __future__ import annotations

from pydantic import BaseModel, RootModel
from tai42_contract.backup import BackupSectionReport


class BackupSectionInfo(BaseModel):
    """One registered backup section: its ``name`` and whether it holds secrets."""

    name: str
    secret: bool


class BackupSectionListing(RootModel[list[BackupSectionInfo]]):
    """The bare-list body of ``list_sections`` — one entry per registered section."""


class BackupImportResult(BaseModel):
    """The import report.

    ``ok`` false when any selected section errored, and the per-section reports
    keyed by section name.
    """

    ok: bool
    sections: dict[str, BackupSectionReport]
