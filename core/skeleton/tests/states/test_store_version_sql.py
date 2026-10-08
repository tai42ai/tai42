"""Every write to a declaration or template row draws a new ``version`` in the statement that changes it.

Every version comes from one sequence, so it only rises and never repeats for a name, even across
a delete and a re-create.

Store writers are driven against the in-memory fake Postgres; the service-level writers (seed,
backup restore, template replace) run the real :class:`StatesService` over the same fake.
"""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import TemplateInUseError
from tai42_contract.states.models import AttachBody, StateDeclaration, StateTemplateDocument

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.store import PostgresStatesStore

from .conftest import FakeStatesPg
from .fake_service_store import _FakeApp

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}}
_EFF = {"type": "object"}


def _version(pg: FakeStatesPg, state: str = "alerts") -> int:
    return pg.declarations[state]["version"]


async def _never(existing, per_kind) -> None:
    return None


async def _declare(store: PostgresStatesStore) -> None:
    await store.upsert_declaration_guarded(
        "alerts", "", _SCHEMA, ["thread"], "thread", None, effective_schema=_SCHEMA, decide=_never
    )


async def test_each_declaration_write_rises_the_version(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    await _declare(store)
    first = _version(pg)
    await _declare(store)
    second = _version(pg)
    await store.upsert_declaration("alerts", "", _SCHEMA, ["thread"], "thread", None)
    assert first < second < _version(pg)


async def test_each_attachment_writer_bumps_the_declaration(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    await _declare(store)
    await store.upsert_template("m", {"name": "m"}, None)
    seen = [_version(pg)]
    await store.upsert_attachment("alerts", "m", [], {}, {}, effective_schema=_EFF)
    seen.append(_version(pg))
    await store.update_attachment_declarations("alerts", "m", {"x": 1}, effective_schema=_EFF)
    seen.append(_version(pg))
    await store.update_attachment_parameters("alerts", "m", {"p": 1}, effective_schema=_EFF)
    seen.append(_version(pg))
    await store.delete_attachment("alerts", "m", effective_schema=_EFF)
    seen.append(_version(pg))
    assert seen == sorted(set(seen))


async def test_template_put_rises_the_template_version(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    await store.upsert_template("m", {"name": "m"}, None)
    first = pg.templates["m"]["version"]
    await store.upsert_template("m", {"name": "m", "description": "x"}, None)
    assert pg.templates["m"]["version"] > first
    assert await store.template_version("m") == pg.templates["m"]["version"]
    assert await store.template_version("absent") is None


async def test_a_recreated_declaration_never_repeats_a_version(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    await _declare(store)
    first = _version(pg)
    assert await store.delete_declaration("alerts")
    await _declare(store)
    assert _version(pg) > first
    await store.upsert_declaration("other", "", _SCHEMA, ["thread"], "thread", None)
    assert await store.delete_declaration("other")
    await store.upsert_declaration("other", "", _SCHEMA, ["thread"], "thread", None)
    assert _version(pg, "other") > _version(pg)


async def test_a_recreated_template_never_repeats_a_version(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    await store.upsert_template("m", {"name": "m"}, None)
    first = pg.templates["m"]["version"]
    assert await store.delete_template("m")
    await store.upsert_template("m", {"name": "m"}, None)
    assert pg.templates["m"]["version"] > first
    assert await store.template_version("m") == pg.templates["m"]["version"]


async def test_version_probes_and_scalars(pg: FakeStatesPg, store: PostgresStatesStore) -> None:
    await _declare(store)
    assert await store.declaration_version("alerts") == _version(pg)
    assert await store.declaration_version("absent") is None
    assert await store.declaration_scalars("alerts") == (_version(pg), ["thread"], "thread")
    assert await store.declaration_scalars("absent") is None


@pytest.fixture
def svc(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=PostgresStatesStore())


def _template(name: str = "tpl", **extra) -> StateTemplateDocument:
    return StateTemplateDocument.model_validate(
        {"kind": "state-template", "name": name, "schema": {"type": "object", "properties": {"y": {}}}, **extra}
    )


_DECL = StateDeclaration(name="alerts", schema=_SCHEMA, subject_kinds=["thread"], default_subject_kind="thread")


async def test_a_seed_is_stored_with_a_version(svc: StatesService, pg: FakeStatesPg) -> None:
    svc.register_template_seed(_template("seeded"))
    await svc.apply_template_seeds()
    assert await svc._store.template_version("seeded") == pg.templates["seeded"]["version"]


async def test_a_backup_restore_of_a_template_bumps_its_version(svc: StatesService, pg: FakeStatesPg) -> None:
    # A backup restore writes a template through ``put_template(replace=True)``.
    await svc.put_template(_template(), replace=False)
    first = pg.templates["tpl"]["version"]
    await svc.put_template(_template(description="restored"), replace=True)
    assert pg.templates["tpl"]["version"] > first


async def test_template_replace_bumps_every_attached_declaration_in_one_transaction(
    svc: StatesService, pg: FakeStatesPg
) -> None:
    for name in ("beta", "alpha"):
        await svc.put_declaration(_DECL.model_copy(update={"name": name}))
    await svc.put_template(_template(), replace=False)
    await svc.attach("beta", "tpl", AttachBody(path=["b"]))
    await svc.attach("alpha", "tpl", AttachBody(path=["a"]))
    before = {name: pg.declarations[name]["version"] for name in ("alpha", "beta")}
    template_before = pg.templates["tpl"]["version"]
    pg.locked.clear()
    await svc.put_template(_template(description="v2"), replace=True)
    assert pg.templates["tpl"]["version"] > template_before
    assert all(pg.declarations[name]["version"] > v for name, v in before.items())
    # The attached declarations are locked, in sorted order, before the template row changes.
    assert pg.locked == [["alpha", "beta"]]
    template_write = max(i for i, (sql, _) in enumerate(pg.executed) if sql.startswith("INSERT INTO state_templates"))
    lock = max(i for i, (sql, _) in enumerate(pg.executed) if "name = ANY" in sql)
    assert lock < template_write


async def test_a_failed_attachment_rewrite_rolls_the_template_back(
    svc: StatesService, pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    await svc.put_declaration(_DECL)
    await svc.put_template(_template(), replace=False)
    await svc.attach("alerts", "tpl", AttachBody(path=["b"]))
    template_before = dict(pg.templates["tpl"])
    version_before = pg.declarations["alerts"]["version"]

    async def failing(*args, **kwargs):
        raise RuntimeError("attachment rewrite failed")

    monkeypatch.setattr(svc._store, "update_attachment_parameters", failing)
    with pytest.raises(RuntimeError, match="attachment rewrite failed"):
        await svc.put_template(_template(description="v2"), replace=True)
    # One transaction: the template body and version are back as they were.
    assert pg.templates["tpl"] == template_before
    assert pg.declarations["alerts"]["version"] == version_before


async def test_a_refused_replace_writes_nothing(svc: StatesService, pg: FakeStatesPg) -> None:
    await svc.put_declaration(_DECL)
    await svc.put_template(_template(), replace=False)
    await svc.attach("alerts", "tpl", AttachBody(path=["b"]))

    async def refusing(state, doc, declarations, effective) -> None:
        from tai42_contract.states.errors import TemplateValidationError

        raise TemplateValidationError(f"refused for {state}")

    svc.register_attach_validator(refusing)
    template_before = dict(pg.templates["tpl"])
    with pytest.raises(TemplateInUseError, match="refused for alerts"):
        await svc.put_template(_template(description="v2"), replace=True)
    assert pg.templates["tpl"] == template_before
