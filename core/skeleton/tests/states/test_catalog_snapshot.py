"""The version-keyed catalog snapshot.

Two :class:`StatesService` instances over one in-memory database stand for two processes: each
keeps its own snapshot, and each serves an entry only for the version its own probe or locked read
returned, so a write through the other is seen on the next call and a delete drops the entry.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import ValueValidationError
from tai42_contract.states.models import AttachBody, StateDeclaration, StateSubject, StateTemplateDocument, WriteOrigin

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.service import catalog as catalog_mod
from tai42_skeleton.states.service.catalog import CatalogSnapshot
from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg
from .fake_service_store import _FakeApp

_ORIGIN = WriteOrigin(consumer="probe")
_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}}


def _decl(description: str = "") -> StateDeclaration:
    return StateDeclaration(
        name="alerts", description=description, schema=_SCHEMA, subject_kinds=["thread"], default_subject_kind="thread"
    )


@pytest.fixture
def services(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=PostgresStatesStore()), StatesService(store=PostgresStatesStore())


def _statements(pg: FakeStatesPg, since: int) -> list[str]:
    return [sql for sql, _ in pg.executed[since:]]


async def test_a_hit_serves_from_one_version_probe(
    services: tuple[StatesService, StatesService], pg: FakeStatesPg
) -> None:
    a, _b = services
    await a.put_declaration(_decl())
    await a.get_declaration("alerts")
    mark = len(pg.executed)
    served = await a.get_declaration("alerts")
    assert served is not None
    assert _statements(pg, mark) == ["SELECT version FROM state_declarations WHERE name = %s"]
    mark = len(pg.executed)
    await a.served_declaration("alerts")
    await a.list_attachments("alerts")
    assert _statements(pg, mark) == ["SELECT version FROM state_declarations WHERE name = %s"] * 2


async def test_a_remote_write_is_seen_on_the_next_call(services: tuple[StatesService, StatesService]) -> None:
    a, b = services
    await a.put_declaration(_decl("first"))
    assert (await a.get_declaration("alerts")).description == "first"  # type: ignore[union-attr]
    await b.put_declaration(_decl("second"))
    assert (await a.get_declaration("alerts")).description == "second"  # type: ignore[union-attr]


async def test_a_remote_attach_is_seen_by_the_write_path(services: tuple[StatesService, StatesService]) -> None:
    a, b = services
    await a.put_declaration(_decl())
    await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    template = StateTemplateDocument.model_validate(
        {
            "kind": "state-template",
            "name": "tpl",
            "schema": {"type": "object", "properties": {"y": {"type": "integer"}}},
        }
    )
    await b.put_template(template, replace=False)
    await b.attach("alerts", "tpl", AttachBody(path=["sub"]))
    # A's next write validates under the effective schema B's attach composed.
    with pytest.raises(ValueValidationError, match="record invalid"):
        await a.replace("alerts", _SUBJECT, {"n": 1, "sub": {"y": "not-an-integer"}}, origin=_ORIGIN)


async def test_a_delete_drops_the_entry(services: tuple[StatesService, StatesService]) -> None:
    a, b = services
    await a.put_declaration(_decl())
    assert await a.get_declaration("alerts") is not None
    await b.delete_declaration("alerts")
    assert await a.get_declaration("alerts") is None
    assert a._catalog.entry_counts()[0] == 0


_INTEGER_N = {"type": "object", "properties": {"n": {"type": "integer"}}}
_STRING_N = {"type": "object", "properties": {"n": {"type": "string"}}}


def _decl_with(schema: dict[str, Any]) -> StateDeclaration:
    return StateDeclaration(name="alerts", schema=schema, subject_kinds=["thread"], default_subject_kind="thread")


def _tpl(description: str, y_type: str) -> StateTemplateDocument:
    return StateTemplateDocument.model_validate(
        {
            "kind": "state-template",
            "name": "tpl",
            "description": description,
            "schema": {"type": "object", "properties": {"y": {"type": y_type}}},
        }
    )


async def test_a_redeclare_after_a_remote_delete_validates_with_the_new_schema(
    services: tuple[StatesService, StatesService],
) -> None:
    a, b = services
    await a.put_declaration(_decl_with(_INTEGER_N))
    await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    await b.delete_declaration("alerts")
    await b.put_declaration(_decl_with(_STRING_N))
    served = await a.get_declaration("alerts")
    assert served is not None
    assert served.schema_ == _STRING_N
    with pytest.raises(ValueValidationError, match="record invalid"):
        await a.replace("alerts", _SUBJECT, {"n": 2}, origin=_ORIGIN)
    await a.replace("alerts", _SUBJECT, {"n": "two"}, origin=_ORIGIN)


async def test_a_redeclare_after_a_local_delete_validates_with_the_new_schema(
    services: tuple[StatesService, StatesService],
) -> None:
    a, _b = services
    await a.put_declaration(_decl_with(_INTEGER_N))
    await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    await a.delete_declaration("alerts")
    await a.put_declaration(_decl_with(_STRING_N))
    with pytest.raises(ValueValidationError, match="record invalid"):
        await a.replace("alerts", _SUBJECT, {"n": 2}, origin=_ORIGIN)


async def test_a_template_recreated_after_a_remote_delete_is_served_anew(
    services: tuple[StatesService, StatesService],
) -> None:
    a, b = services
    await a.put_template(_tpl("one", "integer"), replace=False)
    first = await a.get_rendered_template("tpl")
    assert first is not None
    assert first.description == "one"
    await b.delete_template("tpl")
    await b.put_template(_tpl("two", "string"), replace=False)
    got = await a.get_template("tpl")
    assert got is not None
    assert got.description == "two"
    second = await a.get_rendered_template("tpl")
    assert second is not None
    assert second.description == "two"
    assert second.schema_ == {"type": "object", "properties": {"y": {"type": "string"}}}
    assert second.version != first.version


async def test_a_template_recreated_after_a_local_delete_is_served_anew(
    services: tuple[StatesService, StatesService],
) -> None:
    a, _b = services
    await a.put_template(_tpl("one", "integer"), replace=False)
    await a.get_template("tpl")
    first = await a.get_rendered_template("tpl")
    assert first is not None
    await a.delete_template("tpl")
    await a.put_template(_tpl("two", "string"), replace=False)
    got = await a.get_template("tpl")
    assert got is not None
    assert got.description == "two"
    second = await a.get_rendered_template("tpl")
    assert second is not None
    assert second.description == "two"
    assert second.version != first.version


@pytest.mark.parametrize("door", ["get_template", "get_rendered_template"])
async def test_a_read_overtaken_by_a_newer_read_serves_the_version_it_read(
    services: tuple[StatesService, StatesService], monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    """A slow read holds its row across a remote replace while a second read in the same process
    loads the newer version first; the slow read still answers with the version it read."""
    a, b = services
    await b.put_template(_tpl("one", "integer"), replace=False)
    gate = asyncio.Event()
    real_get = a._store.get_template
    calls = {"n": 0}

    async def slow_first_get(name: str) -> dict[str, Any] | None:
        row = await real_get(name)
        calls["n"] += 1
        if calls["n"] == 1:
            await gate.wait()
        return row

    monkeypatch.setattr(a._store, "get_template", slow_first_get)
    read = getattr(a, door)
    slow = asyncio.create_task(read("tpl"))
    await asyncio.sleep(0)
    await b.put_template(_tpl("two", "integer"), replace=True)
    fast = await read("tpl")
    assert fast is not None
    assert fast.description == "two"
    gate.set()
    held = await slow
    assert held is not None
    assert held.description == "one"
    newest = await read("tpl")
    assert newest is not None
    assert newest.description == "two"


async def test_a_mutated_served_copy_leaves_the_snapshot_unchanged(
    services: tuple[StatesService, StatesService],
) -> None:
    a, _b = services
    await a.put_declaration(_decl())
    first = await a.get_declaration("alerts")
    assert first is not None
    assert isinstance(first.schema_, dict)
    first.schema_["mutated"] = True
    assert first.effective_schema is not None
    first.effective_schema["mutated"] = True
    served = await a.served_declaration("alerts")
    served["schema"]["mutated"] = True
    again = await a.get_declaration("alerts")
    assert again is not None
    assert isinstance(again.schema_, dict)
    assert "mutated" not in again.schema_
    assert again.effective_schema is not None
    assert "mutated" not in again.effective_schema
    assert "mutated" not in (await a.served_declaration("alerts"))["schema"]


async def test_one_validator_per_version(services: tuple[StatesService, StatesService], pg: FakeStatesPg) -> None:
    a, _b = services
    await a.put_declaration(_decl())
    store = a._store
    catalog = a._catalog
    version = pg.declarations["alerts"]["version"]
    _v, _kinds, first = await store.read_apply_context("alerts", catalog=catalog)
    await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    await a.replace("alerts", _SUBJECT, {"n": 2}, origin=_ORIGIN)
    assert catalog.state_at("alerts", version).validator is first.validator  # type: ignore[union-attr]
    # A catalog read at the same version keeps the write path's validator.
    await a.get_declaration("alerts")
    assert catalog.state_at("alerts", version).validator is first.validator  # type: ignore[union-attr]
    await store.upsert_declaration("alerts", "", _SCHEMA, ["thread"], "thread", None)
    await a.replace("alerts", _SUBJECT, {"n": 3}, origin=_ORIGIN)
    bumped = catalog.state_at("alerts", pg.declarations["alerts"]["version"])
    assert bumped is not None
    assert bumped.validator is not first.validator


def test_one_validator_serves_two_threads(services: tuple[StatesService, StatesService], pg: FakeStatesPg) -> None:
    a, _b = services
    asyncio.run(a.put_declaration(_decl()))
    _v, _kinds, entry = asyncio.run(a._store.read_apply_context("alerts", catalog=a._catalog))
    errors: list[BaseException] = []

    def validate(value: Any) -> None:
        try:
            for _ in range(200):
                entry.validator.validate({"n": value})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=validate, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_two_threads_with_their_own_loops_racing_a_miss(
    services: tuple[StatesService, StatesService], pg: FakeStatesPg
) -> None:
    a, _b = services
    asyncio.run(a.put_declaration(_decl()))
    barrier = threading.Barrier(2)
    served: list[Any] = []

    def read() -> None:
        barrier.wait()
        served.append(asyncio.run(a.get_declaration("alerts")))

    threads = [threading.Thread(target=read) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [d.name for d in served] == ["alerts", "alerts"]
    assert a._catalog.state_at("alerts", pg.declarations["alerts"]["version"]) is not None


async def test_the_higher_version_wins_on_insert(
    services: tuple[StatesService, StatesService], pg: FakeStatesPg
) -> None:
    a, _b = services
    await a.put_declaration(_decl())
    catalog = a._catalog
    newer = catalog_mod.build_state_entry(5, _SCHEMA, [], None)
    older = catalog_mod.build_state_entry(4, _SCHEMA, [], None)
    catalog.insert_state("alerts", newer)
    catalog.insert_state("alerts", older)
    assert catalog.state_at("alerts", 5) is newer
    assert catalog.state_at("alerts", 4) is None


async def test_a_failed_validator_build_leaves_no_entry(
    services: tuple[StatesService, StatesService], pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _b = services
    await a.put_declaration(_decl())
    a._catalog = CatalogSnapshot(a._store)
    real = catalog_mod.Draft202012Validator
    calls = {"n": 0}

    def flaky(schema: dict[str, Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("validator build failed")
        return real(schema)

    monkeypatch.setattr(catalog_mod, "Draft202012Validator", flaky)
    with pytest.raises(RuntimeError, match="validator build failed"):
        await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    assert a._catalog.entry_counts() == (0, 0)
    await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    assert calls["n"] == 2


async def test_a_record_read_takes_one_narrow_declaration_row(
    services: tuple[StatesService, StatesService], pg: FakeStatesPg
) -> None:
    a, _b = services
    await a.put_declaration(_decl())
    await a.replace("alerts", _SUBJECT, {"n": 1}, origin=_ORIGIN)
    mark = len(pg.executed)
    await a.read("alerts", _SUBJECT)
    decl_reads = [sql for sql in _statements(pg, mark) if "FROM state_declarations" in sql]
    assert decl_reads == ["SELECT version, subject_kinds, default_subject_kind FROM state_declarations WHERE name = %s"]
