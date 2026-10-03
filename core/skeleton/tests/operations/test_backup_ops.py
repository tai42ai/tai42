"""Op-level oracles for the backup operations — the document-content validation
branches the route round-trips do not reach (they always carry a well-formed
document)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.backup import BackupSectionReport

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.backup.registry import BackupRegistry
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations.backup import import_backup

from .._fakes.bus import FakeBus


class _RecordingResourceManager:
    def __init__(self) -> None:
        self.cleared = False

    def clear_cache(self) -> None:
        self.cleared = True


def _templates_backup(
    monkeypatch: pytest.MonkeyPatch, report: BackupSectionReport, bus: FakeBus
) -> _RecordingResourceManager:
    """Point ``tai42_app`` at a lone ``templates`` section whose importer returns
    ``report``, plus a recording resource manager and ``bus``, so the fleet eviction the
    op fans out after a template restore is assertable."""
    rm = _RecordingResourceManager()

    async def _import_section(name: str, payload: object) -> BackupSectionReport:
        return report

    backup = SimpleNamespace(
        sections=lambda: [SimpleNamespace(name="templates", secret=False)],
        import_section=_import_section,
    )
    monkeypatch.setattr(
        tai42_app, "_impl", SimpleNamespace(backup=backup, storage=SimpleNamespace(resource_manager=rm))
    )
    monkeypatch.setattr(instance.app, "_bus", bus)
    return rm


def _registry_backup(monkeypatch: pytest.MonkeyPatch, registry: BackupRegistry) -> None:
    """Point ``tai42_app`` at a REAL ``BackupRegistry`` so the registry's own validation
    of an importer's result runs under the op (the neutral-consumer proof)."""
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(backup=registry))


async def test_import_rejects_wrong_version() -> None:
    with pytest.raises(BadRequestError, match="unsupported backup document version"):
        await import_backup({"version": 2, "sections": {}}, ["manifest"])


async def test_import_rejects_non_object_sections() -> None:
    # A well-formed envelope whose document carries a non-object ``sections`` is a
    # loud 400 before any section import runs.
    with pytest.raises(BadRequestError, match="document must contain a 'sections' object"):
        await import_backup({"version": 1, "sections": "not-a-dict"}, ["manifest"])


async def test_import_registered_section_absent_from_document_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A selected section this host DOES register, but which the document omits, is a
    # per-section report error (ok=False) — not a transport failure.
    backup = SimpleNamespace(sections=lambda: [SimpleNamespace(name="manifest", secret=False)])
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(backup=backup))

    result = await import_backup({"version": 1, "sections": {}}, ["manifest"])

    assert result.ok is False
    assert "not present in the backup document" in result.sections["manifest"].errors[0]


async def test_template_restore_broadcasts_clear_cache_fleetwide(monkeypatch: pytest.MonkeyPatch) -> None:
    # A template section import writes the store on THIS worker only; without the
    # broadcast every sibling (and a forking backend's prefork children) keeps rendering
    # the pre-restore compilation. The op drops the whole fleet's compiled cache in one
    # ``clear_template_cache`` and the fan-out rides the section report.
    report = BackupSectionReport(created=2, details={"skipped_existing": 0})
    bus = FakeBus(remotes=["serve-w1"])
    rm = _templates_backup(monkeypatch, report, bus)

    result = await import_backup({"version": 1, "sections": {"templates": {"a.j2": "x", "b.j2": "y"}}}, ["templates"])

    assert rm.cleared is True
    assert bus.publish_calls == [
        ({"op": "clear_template_cache"}, None, LocalApplyResult(outcome=OpOutcome.applied, payload=None))
    ]
    assert result.ok is True
    assert result.sections["templates"].fanout is not None
    assert result.sections["templates"].fanout.mode == "fleet"


async def test_template_restore_that_changed_nothing_does_not_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    # A skip-mode restore where every template already exists mutates no store, so the
    # spurious fleet eviction (and its pool turnover) is suppressed.
    report = BackupSectionReport(created=0, updated=0, skipped=0, details={"skipped_existing": 2})
    bus = FakeBus(remotes=["serve-w1"])
    rm = _templates_backup(monkeypatch, report, bus)

    result = await import_backup({"version": 1, "sections": {"templates": {"a.j2": "x", "b.j2": "y"}}}, ["templates"])

    assert rm.cleared is False
    assert bus.publish_calls == []
    assert result.sections["templates"].fanout is None


async def test_neutral_importer_valid_report_is_accepted_and_its_typed_fields_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A neutral (non-vendor) importer returning a valid ``BackupSectionReport`` — its own
    # per-entity counts on the open ``details`` map — is accepted, and the route reads its
    # typed fields: ``ok`` true, the counts and ``details`` surfaced verbatim.
    registry = BackupRegistry()

    def _widgets(_payload: object) -> BackupSectionReport:
        return BackupSectionReport(created=2, updated=1, details={"per_widget": {"a": 2, "b": 1}})

    registry.register_section("widgets", dict, _widgets)
    _registry_backup(monkeypatch, registry)

    result = await import_backup({"version": 1, "sections": {"widgets": {"irrelevant": True}}}, ["widgets"])

    assert result.ok is True
    report = result.sections["widgets"]
    assert (report.created, report.updated, report.skipped, report.errors) == (2, 1, 0, [])
    assert report.details == {"per_widget": {"a": 2, "b": 1}}


async def test_neutral_importer_wrong_shape_is_refused_loudly_under_the_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A neutral importer returning a WRONG shape (a stray top-level field the typed surface
    # forbids) is refused loudly: the registry raises naming the section, the route reports
    # it under that section's errors with ``ok`` false, and the platform reads no count out
    # of the opaque object (the surfaced report carries zero counts).
    registry = BackupRegistry()
    registry.register_section("widgets", dict, lambda _p: {"created": 1, "bogus": True})
    _registry_backup(monkeypatch, registry)

    result = await import_backup({"version": 1, "sections": {"widgets": {}}}, ["widgets"])

    assert result.ok is False
    report = result.sections["widgets"]
    assert report.created == 0
    assert any("widgets" in error for error in report.errors)
