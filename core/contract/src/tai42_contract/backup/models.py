"""Vendor-neutral data shapes for the backup contract.

The backup facet is deliberately shape-agnostic: export/import payloads and
reports are carried as ``Any`` because their concrete shape is a host-side
product concern, and this contract must stay vendor-free. The one typed shape
the facet exposes is the section descriptor listed for the UI.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BackupSectionInfo(BaseModel):
    """Descriptor for one registered backup section, returned by
    ``AppBackup.sections`` for the UI to render.

    ``name`` is the section's unique registration key. ``secret`` marks a
    section whose exported payload carries credentials/secrets, so a caller can
    warn on or gate its handling.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    secret: bool
