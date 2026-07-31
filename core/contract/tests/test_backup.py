"""Tests for the backup contract's typed shape.

The ``AppBackup`` protocol itself is structural — its membership is already
covered by the frozen-facade partition test. Here we pin the one typed model the
facet exposes: :class:`BackupSectionInfo`.
"""

from __future__ import annotations

import pydantic
import pytest


def test_backup_section_info_carries_name_and_secret():
    from tai42_contract.backup import BackupSectionInfo

    info = BackupSectionInfo(name="connectors", secret=True)
    assert info.name == "connectors"
    assert info.secret is True


def test_backup_section_info_is_frozen():
    from tai42_contract.backup import BackupSectionInfo

    info = BackupSectionInfo(name="settings", secret=False)
    with pytest.raises(pydantic.ValidationError):
        info.secret = True  # frozen model — assignment must raise
