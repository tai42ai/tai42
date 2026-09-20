"""The ``states`` backup section's export and import loops, driven with an in-memory fake
store and fake facet doors — no live database.

Export reads the store directly; import writes through the facet doors
(``put_template``/``put_declaration``/``attach``) plus the section's own
``restore_aliases``/``restore_records`` paths, reporting per-entity outcomes. The feature
gate, version guard and registration seam are covered in ``tests/backup/
test_states_section.py``; the full real-store round-trip in
``tests/backup/test_states_section_integration.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.states.errors import StatesError

import tai42_skeleton.app.instance as instance_mod
from tai42_skeleton.states import backup as backup_mod
from tai42_skeleton.states.backup import export_states, import_states


class _FakeExportStore:
    """A store stand-in returning canned rows for the exporter's five reads."""

    def __init__(self) -> None:
        self._templates = [{"body": {"kind": "state-template", "name": "m"}}]
        self._declarations = [
            {
                "name": "alerts",
                "description": "the alerts state",
                "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                "subject_kinds": ["thread"],
                "default_subject_kind": "thread",
                "retention_days": 30,
            }
        ]

    async def list_templates(self) -> list[dict[str, Any]]:
        return self._templates

    async def list_declarations(self) -> list[dict[str, Any]]:
        return self._declarations

    async def list_attachments_for_state(self, state: str) -> list[dict[str, Any]]:
        return [{"template": "m", "path": ["a"], "parameters": {"k": 1}, "declarations": {"d": 2}}]

    async def list_aliases(self, state: str) -> list[dict[str, Any]]:
        return [
            {
                "alias_kind": "thread",
                "alias_key": "old",
                "canonical_kind": "thread",
                "canonical_key": "new",
                "mode": "switch",
            }
        ]

    async def export_records(self, state: str) -> list[dict[str, Any]]:
        return [
            {
                "target_kind": "agent",
                "target_name": "a",
                "subject_kind": "thread",
                "subject_key": "t1",
                "data": {"n": 1},
            }
        ]


async def test_export_states_reads_the_whole_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_mod, "states_store_configured", lambda: True)
    monkeypatch.setattr(backup_mod, "PostgresStatesStore", _FakeExportStore)
    payload = await export_states()
    assert payload["version"] == 1
    assert payload["templates"] == [{"kind": "state-template", "name": "m"}]
    assert payload["declarations"][0]["name"] == "alerts"
    assert payload["declarations"][0]["retention_days"] == 30
    assert payload["attachments"] == [
        {"state": "alerts", "template": "m", "path": ["a"], "parameters": {"k": 1}, "declarations": {"d": 2}}
    ]
    assert payload["aliases"][0]["state"] == "alerts"
    assert payload["aliases"][0]["alias_key"] == "old"
    assert payload["records"][0] == {
        "state": "alerts",
        "target_kind": "agent",
        "target_name": "a",
        "subject_kind": "thread",
        "subject_key": "t1",
        "data": {"n": 1},
    }


class _FakeStatesFacet:
    """A facet stand-in recording import calls; names in ``existing`` read back as present,
    names in ``fail`` raise a :class:`StatesError` from the corresponding door."""

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        fail: set[str] | None = None,
        existing_attachments: set[tuple[str, str]] | None = None,
    ) -> None:
        self.existing = existing or set()
        self.fail = fail or set()
        self.existing_attachments = existing_attachments or set()
        self.put_templates: list[str] = []
        self.put_declarations: list[str] = []
        self.attached: list[tuple[str, str]] = []
        self.updated_attachments: list[tuple[str, str]] = []
        self.skip_reconcilers_seen: list[bool] = []
        self.restored_aliases: list[tuple[str, int]] = []
        self.restored_records: list[tuple[str, int]] = []

    async def get_template(self, name):
        return object() if name in self.existing else None

    async def put_template(self, doc, *, replace):
        if doc.name in self.fail:
            raise StatesError(f"template {doc.name} refused")
        self.put_templates.append(doc.name)

    async def get_declaration(self, name):
        return object() if name in self.existing else None

    async def put_declaration(self, decl):
        if decl.name in self.fail:
            raise StatesError(f"declaration {decl.name} refused")
        self.put_declarations.append(decl.name)

    async def list_attachments(self, state, *, template):
        return [{"state": state, "template": template}] if (state, template) in self.existing_attachments else []

    async def update_attachment_declarations(
        self, state, template, declarations, *, options=None, skip_reconcilers=False
    ):
        self.updated_attachments.append((state, template))
        self.skip_reconcilers_seen.append(skip_reconcilers)

    async def attach(self, state, template, body, *, skip_reconcilers=False):
        if template in self.fail:
            raise StatesError(f"attach {template} refused")
        self.attached.append((state, template))
        self.skip_reconcilers_seen.append(skip_reconcilers)

    async def restore_aliases(self, state, rows, *, origin):
        if state in self.fail:
            raise StatesError(f"aliases for {state} refused")
        self.restored_aliases.append((state, len(rows)))

    async def restore_records(self, state, rows, *, origin):
        if state in self.fail:
            raise StatesError(f"records for {state} refused")
        self.restored_records.append((state, len(rows)))


def _wire(monkeypatch: pytest.MonkeyPatch, facet: _FakeStatesFacet) -> None:
    monkeypatch.setattr(backup_mod, "states_store_configured", lambda: True)
    monkeypatch.setattr(backup_mod, "tai42_app", SimpleNamespace(states=facet))
    monkeypatch.setattr(instance_mod, "app", SimpleNamespace(states=facet))


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "templates": [{"name": "m"}],
        "declarations": [{"name": "alerts", "subject_kinds": ["thread"], "default_subject_kind": "thread"}],
        "attachments": [{"state": "alerts", "template": "m", "path": ["a"], "parameters": {}, "declarations": {}}],
        "aliases": [
            {
                "state": "alerts",
                "alias_kind": "thread",
                "alias_key": "old",
                "canonical_kind": "thread",
                "canonical_key": "new",
                "mode": "switch",
            }
        ],
        "records": [
            {
                "state": "alerts",
                "target_kind": "agent",
                "target_name": "a",
                "subject_kind": "thread",
                "subject_key": "t1",
                "data": {"n": 1},
            }
        ],
    }
    base.update(over)
    return base


async def test_import_creates_all_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = _FakeStatesFacet()
    _wire(monkeypatch, facet)
    report = await import_states(_payload())
    assert report["templates"] == {"created": 1, "updated": 0, "failed": 0}
    assert report["declarations"] == {"created": 1, "updated": 0, "failed": 0}
    assert report["attachments"] == {"created": 1, "updated": 0, "failed": 0}
    assert report["aliases"] == {"restored": 1, "failed": 0}
    assert report["records"] == {"restored": 1, "failed": 0}
    assert report["errors"] == []
    assert facet.attached == [("alerts", "m")]
    # A restored attach is a snapshot, not a re-attach — it must NOT fire the reconcilers.
    assert facet.skip_reconcilers_seen == [True]
    assert facet.restored_records == [("alerts", 1)]


async def test_import_updates_present_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = _FakeStatesFacet(existing={"m", "alerts"}, existing_attachments={("alerts", "m")})
    _wire(monkeypatch, facet)
    report = await import_states(_payload())
    assert report["templates"] == {"created": 0, "updated": 1, "failed": 0}
    assert report["declarations"] == {"created": 0, "updated": 1, "failed": 0}
    assert report["attachments"] == {"created": 0, "updated": 1, "failed": 0}
    assert facet.updated_attachments == [("alerts", "m")]
    assert facet.skip_reconcilers_seen == [True]
    assert facet.attached == []


async def test_import_reports_each_failed_entity_and_skips_it(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = _FakeStatesFacet(fail={"m", "alerts"})
    _wire(monkeypatch, facet)
    report = await import_states(_payload())
    # the template and declaration doors both refuse; the attach refuses (its template failed);
    # the alias and record restores refuse (state "alerts"). Each is reported, none aborts.
    assert report["templates"]["failed"] == 1
    assert report["declarations"]["failed"] == 1
    assert report["attachments"]["failed"] == 1
    assert report["aliases"]["failed"] == 1
    assert report["records"]["failed"] == 1
    assert len(report["errors"]) == 5


async def test_import_groups_alias_and_record_rows_by_state(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = _FakeStatesFacet()
    _wire(monkeypatch, facet)
    payload = _payload(
        aliases=[
            {
                "state": "alerts",
                "alias_kind": "thread",
                "alias_key": "o1",
                "canonical_kind": "thread",
                "canonical_key": "n",
                "mode": "switch",
            },
            {
                "state": "alerts",
                "alias_kind": "thread",
                "alias_key": "o2",
                "canonical_kind": "thread",
                "canonical_key": "n",
                "mode": "switch",
            },
            {
                "state": "status",
                "alias_kind": "thread",
                "alias_key": "o3",
                "canonical_kind": "thread",
                "canonical_key": "n",
                "mode": "switch",
            },
        ],
        records=[
            {
                "state": "alerts",
                "target_kind": "agent",
                "target_name": "a",
                "subject_kind": "thread",
                "subject_key": "t1",
                "data": {"n": 1},
            },
            {
                "state": "status",
                "target_kind": "agent",
                "target_name": "a",
                "subject_kind": "thread",
                "subject_key": "t1",
                "data": {"n": 2},
            },
        ],
        declarations=[],
        templates=[],
        attachments=[],
    )
    report = await import_states(payload)
    assert report["aliases"] == {"restored": 3, "failed": 0}
    assert report["records"] == {"restored": 2, "failed": 0}
    assert sorted(facet.restored_aliases) == [("alerts", 2), ("status", 1)]
    assert sorted(facet.restored_records) == [("alerts", 1), ("status", 1)]


async def test_import_tolerates_absent_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = _FakeStatesFacet()
    _wire(monkeypatch, facet)
    report = await import_states({"version": 1})
    assert report["templates"] == {"created": 0, "updated": 0, "failed": 0}
    assert report["errors"] == []
