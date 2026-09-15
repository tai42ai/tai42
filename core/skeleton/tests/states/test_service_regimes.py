"""The states service's served regimes and the declaration-read projections (updated_at,
retention, the write audit page, the template catalog) — against the in-memory fake store."""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import ValueValidationError
from tai42_contract.states.models import (
    AttachBody,
    StateDeclaration,
    StateTemplateDocument,
    WritesPage,
)

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _STATE, FakeStatesStore, _subject


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> StatesService:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    return StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


_REGIME_TEMPLATE = {
    "kind": "state-template",
    "name": "tagmod",
    "schema": {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
    "regimes": [{"path": ["tags"], "regime": "composing"}],
}


async def test_get_declaration_serves_regimes_for_a_attach_and_empty_for_none(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    # no attachments ⇒ the platform serves an empty regime list, never None-on-the-wire noise
    detached = await svc.get_declaration("alerts")
    assert detached is not None
    assert detached.regimes == []
    # attach a template declaring a composing regime; the served regime is ABSOLUTE (attach
    # path prefixed onto the template's regime path) and matches served_declaration
    await svc.put_template(StateTemplateDocument.model_validate(_REGIME_TEMPLATE), replace=False)
    await svc.attach("alerts", "tagmod", AttachBody(path=["sub"]))
    attached = await svc.get_declaration("alerts")
    assert attached is not None
    assert attached.regimes == [{"path": ["sub", "tags"], "regime": "composing"}]
    served = await svc.served_declaration("alerts")
    assert attached.regimes == served["regimes"]


async def test_list_declarations_serves_composed_regimes(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(StateTemplateDocument.model_validate(_REGIME_TEMPLATE), replace=False)
    await svc.attach("alerts", "tagmod", AttachBody(path=["sub"]))
    decls = await svc.list_declarations()
    assert [d.regimes for d in decls] == [[{"path": ["sub", "tags"], "regime": "composing"}]]


async def test_put_declaration_refuses_a_client_supplied_regimes(svc: StatesService) -> None:
    forged = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
        regimes=[{"path": ["forged"], "regime": "single"}],
    )
    with pytest.raises(ValueError, match="regimes are computed by the platform"):
        await svc.put_declaration(forged)


async def test_declaration_serves_updated_at_on_get_and_list_and_refuses_it_on_put(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    got = await svc.get_declaration("alerts")
    assert got is not None
    assert got.updated_at is not None
    listed = await svc.list_declarations()
    assert [d.updated_at for d in listed] == [got.updated_at]
    forged = StateDeclaration(
        name="alerts",
        schema={"type": "object", "properties": {"n": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
        updated_at="2026-09-06T00:00:00Z",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="updated_at is set by the platform"):
        await svc.put_declaration(forged)


async def test_served_declaration_serves_updated_at_matching_the_list_iso_format(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    served = await svc.served_declaration("alerts")
    listed = (await svc.list_declarations())[0]
    # The single read (GET /api/states/{name}) serves ``updated_at`` in the SAME ISO string
    # the list read serves — one format across both doors, never two.
    assert isinstance(served["updated_at"], str)
    assert served["updated_at"] == listed.model_dump(mode="json")["updated_at"]


async def test_served_declaration_serves_retention_days_matching_the_list_read(svc: StatesService) -> None:
    # A configured retention must ride the single read (GET /api/states/{name}) so an edit
    # form round-trips it; omitting it would let a full-declaration PUT clear the setting.
    await svc.put_declaration(
        StateDeclaration(
            name="alerts",
            schema={"type": "object", "properties": {"n": {"type": "integer"}}},
            subject_kinds=["thread"],
            default_subject_kind="thread",
            retention_days=30,
        )
    )
    served = await svc.served_declaration("alerts")
    listed = (await svc.list_declarations())[0]
    assert served["retention_days"] == 30
    assert served["retention_days"] == listed.model_dump(mode="json")["retention_days"]


async def test_writes_returns_a_keyset_page_with_next_cursor(svc: StatesService) -> None:
    from datetime import UTC, datetime

    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    subject = _subject()
    key = ("alerts", subject.target_kind, subject.target_name, subject.kind, subject.key)
    store.write_rows[key] = [
        {
            "id": rid,
            "seq": float(rid),
            "at": datetime(2026, 1, 1, tzinfo=UTC),
            "door": "api",
            "actor": None,
            "consumer": None,
            "meta": None,
            "run_id": None,
            "op_id": None,
            "turn_id": None,
            "paths": [["n"]],
        }
        for rid in (3, 2, 1)
    ]
    first = await svc.writes("alerts", subject, limit=2)
    assert isinstance(first, WritesPage)
    assert [e.seq for e in first.items] == [3.0, 2.0]
    # A full page hands back the last row's id as the cursor for the next call.
    assert first.next_cursor == "2"
    second = await svc.writes("alerts", subject, limit=2, cursor=first.next_cursor)
    assert [e.seq for e in second.items] == [1.0]
    # The last page is exhausted, so it carries no cursor.
    assert second.next_cursor is None


async def test_writes_refuses_a_malformed_cursor_with_a_value_error(svc: StatesService) -> None:
    # A client-supplied opaque cursor that is not a row id is a 422 (ValueValidationError →
    # ValidationRejectedError at the door), never a 500 from ``int()`` deep in the store.
    with pytest.raises(ValueValidationError, match="cursor"):
        await svc.writes("alerts", _subject(), limit=2, cursor="not-a-row-id")


async def test_list_templates_catalog_adds_attached_to_and_shipped_default(svc: StatesService) -> None:
    await svc.put_declaration(_STATE)
    await svc.put_template(StateTemplateDocument.model_validate(_REGIME_TEMPLATE), replace=False)
    await svc.attach("alerts", "tagmod", AttachBody(path=["sub"]))
    # A second, operator-uploaded template (no shipped_hash) that is attached nowhere.
    store: FakeStatesStore = svc._store  # type: ignore[assignment]
    store.templates["loose"] = {
        "name": "loose",
        "body": {"kind": "state-template", "name": "loose", "schema": {"type": "object"}},
        "shipped_hash": None,
        "updated_at": 1,
    }
    # Mark the attached template as an unedited shipped default.
    store.templates["tagmod"]["shipped_hash"] = "abc123"
    catalog = {row["name"]: row for row in await svc.list_templates_catalog()}
    assert catalog["tagmod"]["attached_to"] == 1
    assert catalog["tagmod"]["shipped_default"] is True
    assert catalog["loose"]["attached_to"] == 0
    assert catalog["loose"]["shipped_default"] is False
