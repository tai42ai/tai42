"""Vendor-neutral backup contract.

A plugin (or the host itself, the first consumer) registers a named backup
section supplying an exporter/importer pair; the facet lists sections for the UI
and runs one section's export/import. Export payloads are shape-agnostic
(``Any``); a section's import report is the typed :class:`BackupSectionReport`,
and the section descriptor is :class:`BackupSectionInfo`.
"""

from __future__ import annotations

from tai42_contract.backup.models import BackupSectionInfo, BackupSectionReport

__all__ = [
    "BackupSectionInfo",
    "BackupSectionReport",
]
