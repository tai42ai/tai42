"""Vendor-neutral backup contract.

A plugin (or the host itself, the first consumer) registers a named backup
section supplying an exporter/importer pair; the facet lists sections for the UI
and runs one section's export/import. Payloads and reports are shape-agnostic
(``Any``); the one typed shape is :class:`BackupSectionInfo`.
"""

from __future__ import annotations

from tai42_contract.backup.models import BackupSectionInfo

__all__ = [
    "BackupSectionInfo",
]
