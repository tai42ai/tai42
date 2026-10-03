"""The concrete ``BackupRegistry`` — the impl body behind the ``app.backup`` facet.

Pins the registry contract directly (no app, no HTTP): registration order,
the duplicate-name guard, the unknown-name raises, that an exporter runs through
verbatim, and that ``import_section`` validates the importer's result into a
:class:`BackupSectionReport` — awaiting an async importer and raising
:class:`BackupSectionReportError` naming the section on a wrong shape.
"""

from __future__ import annotations

import pytest
from tai42_contract.backup import BackupSectionInfo, BackupSectionReport

from tai42_skeleton.backup.registry import BackupRegistry, BackupSectionReportError


def test_sections_reports_registration_order_and_secret_flag():
    registry = BackupRegistry()
    registry.register_section("alpha", lambda: 1, lambda _p: {}, secret=True)
    registry.register_section("beta", lambda: 2, lambda _p: {})

    assert registry.sections() == [
        BackupSectionInfo(name="alpha", secret=True),
        BackupSectionInfo(name="beta", secret=False),
    ]


def test_register_duplicate_name_raises():
    registry = BackupRegistry()
    registry.register_section("dup", lambda: 1, lambda _p: {})
    with pytest.raises(ValueError, match="already registered"):
        registry.register_section("dup", lambda: 2, lambda _p: {})


def test_export_section_runs_exporter():
    registry = BackupRegistry()
    registry.register_section("s", lambda: {"value": 42}, lambda _p: {})
    assert registry.export_section("s") == {"value": 42}


async def test_import_section_runs_importer_with_payload():
    registry = BackupRegistry()
    seen: dict = {}

    def _importer(payload):
        seen["payload"] = payload
        return {"created": 1}

    registry.register_section("s", lambda: None, _importer)
    assert await registry.import_section("s", {"a": 1}) == BackupSectionReport(created=1)
    assert seen["payload"] == {"a": 1}


async def test_import_section_awaits_async_importer_and_returns_typed_report():
    registry = BackupRegistry()

    async def _importer(_payload):
        return BackupSectionReport(created=2, details={"skipped_existing": 1})

    registry.register_section("s", lambda: None, _importer)
    report = await registry.import_section("s", {})
    assert report == BackupSectionReport(created=2, details={"skipped_existing": 1})


async def test_import_section_refuses_wrong_shape_naming_the_section():
    # An importer that returns a non-report shape — a stray top-level field the typed
    # surface forbids — is refused loudly, the error naming the section.
    registry = BackupRegistry()
    registry.register_section("s", lambda: None, lambda _p: {"created": 1, "bogus": True})
    with pytest.raises(BackupSectionReportError, match="'s'"):
        await registry.import_section("s", {})


def test_export_unknown_section_raises():
    registry = BackupRegistry()
    with pytest.raises(KeyError, match="unknown backup section"):
        registry.export_section("nope")


async def test_import_unknown_section_raises():
    registry = BackupRegistry()
    with pytest.raises(KeyError, match="unknown backup section"):
        await registry.import_section("nope", {})
