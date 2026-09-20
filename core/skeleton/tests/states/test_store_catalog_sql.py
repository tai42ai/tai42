"""The ``state_templates`` and ``state_attachments`` SQL — template reads/upsert/delete, attach
reads, and the effective-schema-recomposing attach writes — driven against the in-memory fake
Postgres (the ``pg``/``store`` fixtures in ``conftest``).
"""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import StateNotFoundError

from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg


async def test_template_upsert_get_list_delete(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    assert await store.get_template("m") is None
    await store.upsert_template("m", {"name": "m"}, "hash-1")
    row = await store.get_template("m")
    assert row is not None
    assert row["shipped_hash"] == "hash-1"
    await store.upsert_template("m", {"name": "m", "v": 2}, None)  # operator upload clears the hash
    updated = await store.get_template("m")
    assert updated is not None
    assert updated["shipped_hash"] is None
    await store.upsert_template("a", {"name": "a"}, None)
    assert [r["name"] for r in await store.list_templates()] == ["a", "m"]
    assert await store.delete_template("m") is True
    assert await store.delete_template("m") is False


async def test_attached_template_counts(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    for state, template in (("s1", "m1"), ("s2", "m1"), ("s1", "m2")):
        pg.attachments[(state, template)] = {
            "state": state,
            "template": template,
            "path": [],
            "parameters": {},
            "declarations": {},
            "updated_at": pg.tick(),
        }
    assert await store.attached_template_counts() == {"m1": 2, "m2": 1}


async def test_attach_reads(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    for state, template in (("s1", "m2"), ("s1", "m1"), ("s2", "m1")):
        pg.attachments[(state, template)] = {
            "state": state,
            "template": template,
            "path": ["p"],
            "parameters": {"k": 1},
            "declarations": {},
            "updated_at": pg.tick(),
        }
    assert await store.get_attachment("s1", "m1") is not None
    assert await store.get_attachment("s1", "nope") is None
    assert [r["template"] for r in await store.list_attachments_for_state("s1")] == ["m1", "m2"]
    assert [r["state"] for r in await store.list_attachments_of_template("m1")] == ["s1", "s2"]
    assert [(r["state"], r["template"]) for r in await store.list_all_attachments()] == [
        ("s1", "m1"),
        ("s1", "m2"),
        ("s2", "m1"),
    ]


async def test_upsert_attach_writes_row_and_effective_schema(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    eff = {"type": "object", "properties": {"a": {"type": "object"}}}
    await store.upsert_attachment("alerts", "m", ["a"], {"k": 1}, {"d": 2}, effective_schema=eff)
    assert pg.attachments[("alerts", "m")]["path"] == ["a"]
    assert pg.declarations["alerts"]["effective_schema"] == eff
    # a second upsert on the same (state, template) updates in place
    await store.upsert_attachment("alerts", "m", ["b"], {}, {}, effective_schema=eff)
    assert pg.attachments[("alerts", "m")]["path"] == ["b"]


async def test_upsert_attach_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.upsert_attachment("nope", "m", ["a"], {}, {}, effective_schema={})


async def test_update_attach_declarations(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {"old": 1},
        "updated_at": pg.tick(),
    }
    eff = {"type": "object", "properties": {"a": {"type": "object"}}}
    assert await store.update_attachment_declarations("alerts", "m", {"new": 2}, effective_schema=eff) is True
    assert pg.attachments[("alerts", "m")]["declarations"] == {"new": 2}
    assert pg.declarations["alerts"]["effective_schema"] == eff
    # no such attach → False (and the declaration lock passed since the state exists)
    assert await store.update_attachment_declarations("alerts", "absent", {}, effective_schema=eff) is False


async def test_update_attach_declarations_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.update_attachment_declarations("nope", "m", {}, effective_schema={})


async def test_update_attach_parameters(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {"k": 1},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    eff = {"type": "object", "properties": {"a": {"type": "object"}}}
    assert await store.update_attachment_parameters("alerts", "m", {"k": 2}, effective_schema=eff) is True
    assert pg.attachments[("alerts", "m")]["parameters"] == {"k": 2}
    assert await store.update_attachment_parameters("alerts", "absent", {}, effective_schema=eff) is False


async def test_update_attach_parameters_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.update_attachment_parameters("nope", "m", {}, effective_schema={})


async def test_delete_attach(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    pg.seed_declaration("alerts")
    pg.attachments[("alerts", "m")] = {
        "state": "alerts",
        "template": "m",
        "path": ["a"],
        "parameters": {},
        "declarations": {},
        "updated_at": pg.tick(),
    }
    eff = {"type": "object", "properties": {}}
    assert await store.delete_attachment("alerts", "m", effective_schema=eff) is True
    assert ("alerts", "m") not in pg.attachments
    assert pg.declarations["alerts"]["effective_schema"] == eff
    assert await store.delete_attachment("alerts", "m", effective_schema=eff) is False


async def test_delete_attach_undeclared_raises(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    with pytest.raises(StateNotFoundError):
        await store.delete_attachment("nope", "m", effective_schema={})
