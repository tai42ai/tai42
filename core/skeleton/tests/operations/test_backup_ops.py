"""Op-level oracles for the backup operations — the document-content validation
branches the route round-trips do not reach (they always carry a well-formed
document)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations.backup import import_backup
from tests._fakes.bus import FakeBus


class _RecordingResourceManager:
    def __init__(self) -> None:
        self.cleared = False

    def clear_cache(self) -> None:
        self.cleared = True


def _templates_backup(monkeypatch: pytest.MonkeyPatch, report: dict, bus: FakeBus) -> _RecordingResourceManager:
    """Point ``tai42_app`` at a lone ``templates`` section whose importer returns
    ``report``, plus a recording resource manager and ``bus``, so the fleet eviction the
    op fans out after a template restore is assertable."""
    rm = _RecordingResourceManager()
    backup = SimpleNamespace(
        sections=lambda: [SimpleNamespace(name="templates", secret=False)],
        import_section=lambda name, payload: report,
    )
    monkeypatch.setattr(
        tai42_app, "_impl", SimpleNamespace(backup=backup, storage=SimpleNamespace(resource_manager=rm))
    )
    monkeypatch.setattr(instance.app, "_bus", bus)
    return rm


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

    assert result["ok"] is False
    assert "not present in the backup document" in result["sections"]["manifest"]["errors"][0]


async def test_template_restore_broadcasts_clear_cache_fleetwide(monkeypatch: pytest.MonkeyPatch) -> None:
    # A template section import writes the store on THIS worker only; without the
    # broadcast every sibling (and a forking backend's prefork children) keeps rendering
    # the pre-restore compilation. The op drops the whole fleet's compiled cache in one
    # ``clear_template_cache`` and the fan-out rides the section report.
    report = {"created": 2, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    bus = FakeBus(remotes=["serve-w1"])
    rm = _templates_backup(monkeypatch, report, bus)

    result = await import_backup({"version": 1, "sections": {"templates": {"a.j2": "x", "b.j2": "y"}}}, ["templates"])

    assert rm.cleared is True
    assert bus.publish_calls == [
        ({"op": "clear_template_cache"}, None, LocalApplyResult(outcome=OpOutcome.applied, payload=None))
    ]
    assert result["ok"] is True
    assert result["sections"]["templates"]["fanout"]["mode"] == "fleet"


async def test_template_restore_that_changed_nothing_does_not_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    # A skip-mode restore where every template already exists mutates no store, so the
    # spurious fleet eviction (and its pool turnover) is suppressed.
    report = {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 2, "errors": []}
    bus = FakeBus(remotes=["serve-w1"])
    rm = _templates_backup(monkeypatch, report, bus)

    result = await import_backup({"version": 1, "sections": {"templates": {"a.j2": "x", "b.j2": "y"}}}, ["templates"])

    assert rm.cleared is False
    assert bus.publish_calls == []
    assert "fanout" not in result["sections"]["templates"]
