"""Op-level oracles for the backup operations — the document-content validation
branches the route round-trips do not reach (they always carry a well-formed
document)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import RecycleRoll
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


def _storage_backup(
    monkeypatch: pytest.MonkeyPatch, report: BackupSectionReport, bus: FakeBus
) -> _RecordingResourceManager:
    """Point ``tai42_app`` at a lone ``storage`` section whose importer returns
    ``report``, plus a recording resource manager and ``bus``, so the fleet eviction the
    op fans out after a store restore is assertable."""
    rm = _RecordingResourceManager()

    async def _import_section(name: str, payload: object) -> BackupSectionReport:
        return report

    backup = SimpleNamespace(
        sections=lambda: [SimpleNamespace(name="storage", secret=False)],
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


async def test_storage_restore_broadcasts_clear_cache_fleetwide(monkeypatch: pytest.MonkeyPatch) -> None:
    # A storage section import writes the store on THIS worker only; without the
    # broadcast every sibling (and a forking backend's prefork children) keeps rendering
    # the pre-restore compilation. The op drops the whole fleet's compiled cache in one
    # ``clear_template_cache`` and the fan-out rides the section report.
    report = BackupSectionReport(created=2, details={"skipped_existing": 0})
    bus = FakeBus(remotes=["serve-w1"])
    rm = _storage_backup(monkeypatch, report, bus)

    result = await import_backup(
        {"version": 1, "sections": {"storage": {"a.j2": {"content_text": "x"}, "b.j2": {"content_text": "y"}}}},
        ["storage"],
    )

    assert rm.cleared is True
    assert bus.publish_calls == [
        ({"op": "clear_template_cache"}, None, LocalApplyResult(outcome=OpOutcome.applied, payload=None))
    ]
    assert result.ok is True
    assert result.sections["storage"].fanout is not None
    assert result.sections["storage"].fanout.mode == "fleet"


async def test_storage_restore_that_changed_nothing_does_not_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    # A skip-mode restore where every object already exists mutates no store, so the
    # spurious fleet eviction (and its pool turnover) is suppressed.
    report = BackupSectionReport(created=0, updated=0, skipped=0, details={"skipped_existing": 2})
    bus = FakeBus(remotes=["serve-w1"])
    rm = _storage_backup(monkeypatch, report, bus)

    result = await import_backup(
        {"version": 1, "sections": {"storage": {"a.j2": {"content_text": "x"}, "b.j2": {"content_text": "y"}}}},
        ["storage"],
    )

    assert rm.cleared is False
    assert bus.publish_calls == []
    assert result.sections["storage"].fanout is None


async def test_a_document_section_named_templates_is_an_unknown_section_and_storage_still_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = BackupSectionReport(created=1, details={"skipped_existing": 0})
    rm = _storage_backup(monkeypatch, report, FakeBus())
    rows = {"a.j2": {"content_text": "x"}}

    result = await import_backup(
        {"version": 1, "sections": {"templates": rows, "storage": rows}}, ["templates", "storage"]
    )

    assert result.ok is False
    assert result.sections["templates"].errors == ["unknown section: 'templates'"]
    assert result.sections["storage"].created == 1
    assert result.sections["storage"].errors == []
    assert rm.cleared is True


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


def _roll(*statuses: str) -> RecycleRoll:
    return RecycleRoll.model_validate(
        {
            "recycle": [
                {"name": f"w-{i}", "kind": "serve", "status": status, "generation_before": 1, "keys": ["TAI_X"]}
                for i, status in enumerate(statuses)
            ],
            "skipped": [],
            "fresh": [],
            "recycle_stopped": None,
        }
    )


def _writer_moved_decision(writer: Any) -> Any:
    """The decision of a reload whose writer and its backend siblings report BACKEND_MANIFEST_KEY moved."""
    from tai42_skeleton.config.recycle_policy import RecycleDecision, RecycleReason, ReportedKeys

    return RecycleDecision(
        writer=writer,
        reported=dict.fromkeys(("backend-1", "backend-2"), ReportedKeys(generation=1, keys=("BACKEND_MANIFEST_KEY",))),
        unconfirmed={},
        clean=frozenset(),
        writer_reason=RecycleReason(keys=("BACKEND_MANIFEST_KEY",)),
        withheld={},
        addressed=None,
        report_unreachable=False,
    )


async def test_the_env_section_restore_reports_the_recycle_roll_its_env_change_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
    from tai42_skeleton.backup import sections
    from tai42_skeleton.config.service import ApplyResult, ConfigService

    result = ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 1, "awaiting_respawn": []},
        recycle=RecycleReport(
            rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)]
        ),
        decision=_writer_moved_decision(
            WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=2)
        ),
        self_exit_armed=True,
    )

    class _Svc:
        async def apply_env_change(self, changes: dict[str, str]) -> ApplyResult:
            return result

    manager = SimpleNamespace(read_env=lambda: {"BACKEND_MANIFEST_KEY": "OLD"})
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(config=SimpleNamespace(config_manager=manager)))
    monkeypatch.setattr(instance.app, "_bus", FakeBus(origin="serve-writer"))
    monkeypatch.setattr(ConfigService, "from_app", classmethod(lambda cls: _Svc()))

    report = await sections._import_env({"BACKEND_MANIFEST_KEY": "NEW"})

    assert report.recycle is not None
    assert [entry.status for entry in report.recycle.recycle] == ["recycled", "self-deferred"]


async def test_the_env_section_restore_reports_the_workers_still_awaiting_a_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import TIMED_OUT, RecycleReport, RecycleRow, RecycleStop
    from tai42_skeleton.backup import sections
    from tai42_skeleton.config.service import ApplyResult, ConfigService

    verdict = [
        AwaitingWorker(name="backend-2", keys=["BACKEND_MANIFEST_KEY"]),
        AwaitingWorker(name="serve-writer", keys=["BACKEND_MANIFEST_KEY"]),
    ]
    result = ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 1, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]},
        recycle=RecycleReport(
            rows=[RecycleRow(name="backend-2", kind="backend", generation_before=1, status=TIMED_OUT, detail="slow")],
            stopped=RecycleStop(kind="backend", name="backend-2", detail="slow"),
        ),
        decision=_writer_moved_decision(
            WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=2)
        ),
        awaiting_respawn=verdict,
    )

    class _Svc:
        async def apply_env_change(self, changes: dict[str, str]) -> ApplyResult:
            return result

    manager = SimpleNamespace(read_env=lambda: {"BACKEND_MANIFEST_KEY": "OLD"})
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(config=SimpleNamespace(config_manager=manager)))
    monkeypatch.setattr(instance.app, "_bus", FakeBus(origin="serve-writer"))
    monkeypatch.setattr(ConfigService, "from_app", classmethod(lambda cls: _Svc()))

    report = await sections._import_env({"BACKEND_MANIFEST_KEY": "NEW"})

    assert report.awaiting_respawn == verdict


def _converged_roll() -> Any:
    """A roll that converged on ``backend-1`` with the writer ``serve-writer`` needing its own restart."""
    from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
    from tai42_skeleton.config.recycle_roll import RollOutcome

    return RollOutcome(
        decision=_writer_moved_decision(
            WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=2)
        ),
        recycle=RecycleReport(
            rows=[
                RecycleRow(
                    name="backend-1",
                    kind="backend",
                    generation_before=1,
                    status=RECYCLED,
                    keys=["BACKEND_MANIFEST_KEY"],
                )
            ]
        ),
        awaiting_respawn=[],
        self_exit_armed=True,
    )


def _owe_writer_exit(change: str) -> None:
    """Stand in for an armed roll: hold the writer's exit and owe it into the open scope."""
    from tai42_skeleton.app.writer_exit import open_writer_exit_scope

    bus = instance.app.bus
    scope = open_writer_exit_scope(change)
    scope.hold_exit(bus, change)
    scope.owe(bus)


def _landed_but_unpropagated(op_name: str) -> Any:
    """The error of a change that landed, whose roll converged armed, and whose fleet propagation failed."""
    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    cause = ConnectionError("bus lost mid-broadcast")
    error = FleetBroadcastError(op_name, FleetResult(op="reload_config", reachable=False), cause)
    error.roll = _converged_roll()
    return error


def _config_service(monkeypatch: pytest.MonkeyPatch, service: object) -> FakeBus:
    """Serve ``service`` as the app's :class:`ConfigService` over a serve writer's bus; that bus."""
    from tai42_skeleton.config.service import ConfigService

    manager = SimpleNamespace(read_env=lambda: {"BACKEND_MANIFEST_KEY": "OLD"})
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(config=SimpleNamespace(config_manager=manager)))
    bus = FakeBus(origin="serve-writer")
    monkeypatch.setattr(instance.app, "_bus", bus)
    monkeypatch.setattr(ConfigService, "from_app", classmethod(lambda cls: service))
    return bus


async def test_an_env_section_whose_change_landed_but_did_not_propagate_reports_its_roll_and_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, writer_exit_scope
    from tai42_skeleton.backup import sections

    class _Svc:
        async def apply_env_change(self, changes: dict[str, str]) -> object:
            _owe_writer_exit("the env restore")
            raise _landed_but_unpropagated("reload_config")

    bus = _config_service(monkeypatch, _Svc())

    with caplog.at_level("WARNING"), writer_exit_scope() as exits:
        report = await sections._import_env({"BACKEND_MANIFEST_KEY": "NEW", "TAI_NEW": "1"})
        await exits.settle()

    # The change landed: its counts stand, next to its failure, its fleet report and its roll.
    assert (report.created, report.updated) == (1, 1)
    assert len(report.errors) == 1
    assert "fleet propagation failed" in report.errors[0]
    assert report.fanout is not None
    assert report.fanout.reachable is False
    assert report.recycle is not None
    assert [(e.name, e.status) for e in report.recycle.recycle] == [
        ("backend-1", "recycled"),
        ("serve-writer", "self-deferred"),
    ]
    assert report.awaiting_respawn == []
    assert any("backup import of section 'env'" in r.getMessage() and r.exc_info for r in caplog.records)
    # The roll armed the writer's exit: it runs, and the change's hold is released.
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None


async def test_the_manifest_section_restore_reports_the_recycle_roll_its_change_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app.bus import FleetResult
    from tai42_skeleton.backup import sections
    from tai42_skeleton.config.service import ApplyResult

    roll = _converged_roll()
    result = ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 1, "awaiting_respawn": []},
    ).rolled(roll)

    class _Svc:
        async def apply_replace(self, manifest: dict[str, Any]) -> ApplyResult:
            return result

    _config_service(monkeypatch, _Svc())

    report = await sections._import_manifest({"tools": []})

    assert report.updated == 1
    assert report.recycle is not None
    assert [entry.status for entry in report.recycle.recycle] == ["recycled", "self-deferred"]
    assert report.awaiting_respawn == []


async def test_a_manifest_section_whose_change_landed_but_did_not_propagate_reports_its_roll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.backup import sections

    class _Svc:
        async def apply_replace(self, manifest: dict[str, Any]) -> object:
            raise _landed_but_unpropagated("replace_manifest")

    _config_service(monkeypatch, _Svc())

    report = await sections._import_manifest({"tools": []})

    assert report.updated == 1
    assert len(report.errors) == 1
    assert report.recycle is not None
    assert [entry.status for entry in report.recycle.recycle] == ["recycled", "self-deferred"]


async def test_a_projected_env_restore_whose_roll_owes_the_writers_exit_runs_it_before_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_contract.manifest import ApiToolsConfig

    from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON
    from tai42_skeleton.operations import OperationRegistry, operation_metadata_of
    from tai42_skeleton.operations.projection import project_operations

    roll = _roll("recycled", "self-deferred")

    async def _import_section(name: str, payload: object) -> BackupSectionReport:
        _owe_writer_exit("the env restore")
        return BackupSectionReport(updated=1, recycle=roll)

    backup = SimpleNamespace(
        sections=lambda: [SimpleNamespace(name="env", secret=True)], import_section=_import_section
    )
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(backup=backup))
    bus = FakeBus(origin="w-1")
    monkeypatch.setattr(instance.app, "_bus", bus)
    reg = OperationRegistry()
    reg.register(operation_metadata_of(import_backup))
    registered: dict[str, object] = {}

    class _Tools:
        def tool(self, *, force, name, tags, annotations):  # type: ignore[no-untyped-def]
            def decorator(func):  # type: ignore[no-untyped-def]
                registered[name] = func
                return func

            return decorator

    project_operations(
        SimpleNamespace(tools=_Tools()),
        ApiToolsConfig(include=["import_backup"], expose_destructive=True),
        registry=reg,
    )
    reply = await registered["import_backup"](  # type: ignore[operator]
        document={"version": 1, "sections": {"env": {"BACKEND_MANIFEST_KEY": "NEW"}}}, sections=["env"]
    )

    assert reply.sections["env"].recycle.recycle[-1].status == "self-deferred"
    assert bus.exit_requests == [WRITER_EXIT_REASON]
