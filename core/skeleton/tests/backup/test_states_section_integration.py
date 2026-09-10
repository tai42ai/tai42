"""A REAL Postgres round-trip of the ``states`` backup section: export the store, then
import it back through the facet doors in the plan's order (modules → declarations →
mounts → aliases → records), asserting the per-entity created/updated/failed counts and
that one refused entity is reported while the rest still lands.

The gate/version/registration seams are pinned without a live store in
``test_states_section.py``; this exercise needs the real facet + Postgres so the import's
validation, ordering and provenance run exactly as production. It is OPT-IN: set
``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres.
Without the opt-in it SKIPS VISIBLY with a clear reason (never a silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, LiteralString

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.models import (
    AttachBody,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    WriteOrigin,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.states.backup import export_states, import_states
from tai42_skeleton.states.db import STATES_COMPONENT, states_entry

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"

_BASE_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}}
_MODULE_SCHEMA = {"type": "object", "properties": {"m": {"type": "string"}}}
_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
_ORIGIN = WriteOrigin(consumer="consumer-x")


# Names: a state may carry ``_`` (STATE_NAME_RE), a module may NOT (TEMPLATE_NAME_RE: hyphens
# only). Both derive from one per-run token so a shared database stays isolated and teardown
# is exact.
def _state(token: str) -> str:
    return f"st-{token}"


def _module(token: str) -> str:
    return f"mod-{token}"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(STATES_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


async def _wipe(token: str) -> None:
    """Delete every row this run wrote — the state-columned tables by ``state`` and the two
    global ledgers (``state_templates`` and ``state_applied_ops``, both keyed by names/ids
    carrying the run token) — so a re-run starts clean."""
    state = _state(token)
    await _exec("DELETE FROM state_writes WHERE state = %s", (state,))
    await _exec("DELETE FROM state_subject_aliases WHERE state = %s", (state,))
    await _exec("DELETE FROM state_records WHERE state = %s", (state,))
    await _exec("DELETE FROM state_attachments WHERE state = %s", (state,))
    await _exec("DELETE FROM state_applied_ops WHERE op_id LIKE %s", (f"%{token}%",))
    await _exec("DELETE FROM state_templates WHERE name LIKE %s", (f"%{token}%",))
    await _exec("DELETE FROM state_declarations WHERE name = %s", (state,))


@pytest.fixture
async def run_token() -> AsyncIterator[str]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres states backup round-trip is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the facet + jsonb — no fake)"
        )
    reset_all_settings()
    await apply_migrations([states_entry()])
    token = uuid.uuid4().hex[:12]
    await _wipe(token)
    # The section's import writes through the ``tai42_app.states`` facet doors, so a real
    # app must be bound for the duration of the exercise.
    with tai42_app.bound(instance.build_app()):
        yield token
    await _wipe(token)


async def _seed(token: str) -> None:
    """One of each entity the section carries, through the facet doors — the export's
    source and the round-trip's expectation."""
    state, module = _state(token), _module(token)
    await tai42_app.states.put_declaration(
        StateDeclaration(name=state, schema=_BASE_SCHEMA, subject_kinds=["thread"], default_subject_kind="thread")
    )
    await tai42_app.states.put_template(
        StateTemplateDocument.model_validate({"kind": "state-template", "name": module, "schema": _MODULE_SCHEMA}),
        replace=False,
    )
    await tai42_app.states.attach(state, module, AttachBody(path=["a"], parameters={}, declarations={}))
    await tai42_app.states.replace(state, _SUBJECT, {"n": 1}, origin=_ORIGIN)
    await instance.app.states.restore_aliases(
        state,
        [
            {
                "target_kind": "agent",
                "target_name": "a",
                "alias_kind": "thread",
                "alias_key": "old",
                "canonical_kind": "thread",
                "canonical_key": "t1",
                "mode": "switch",
            }
        ],
        origin=_ORIGIN,
    )


def _scope_payload(payload: dict[str, Any], token: str) -> dict[str, Any]:
    """The export payload narrowed to this run's rows — the shared database may hold other
    states' entities the round-trip must not touch."""
    state, module = _state(token), _module(token)
    return {
        "version": payload["version"],
        "templates": [m for m in payload["templates"] if m.get("name") == module],
        "declarations": [d for d in payload["declarations"] if d["name"] == state],
        "attachments": [mo for mo in payload["attachments"] if mo["state"] == state],
        "aliases": [a for a in payload["aliases"] if a["state"] == state],
        "records": [r for r in payload["records"] if r["state"] == state],
    }


async def test_backup_round_trip_through_facet_doors(run_token: str) -> None:
    token = run_token
    state, module = _state(token), _module(token)
    await _seed(token)

    payload = await export_states()
    # the export carries this run's one of each entity (a shared database may hold others)
    assert any(d["name"] == state for d in payload["declarations"])
    assert any(m.get("name") == module for m in payload["templates"])
    assert any(mo["state"] == state for mo in payload["attachments"])
    assert any(a["state"] == state and a["alias_key"] == "old" for a in payload["aliases"])
    assert any(r["state"] == state and r["subject_key"] == "t1" for r in payload["records"])

    # scope the payload to THIS run's rows so the counts below are exact on a shared database
    scoped = _scope_payload(payload, token)

    # drop every row, then import the scoped payload — each entity is CREATED
    await _wipe(token)
    created = await import_states(scoped)
    assert created["errors"] == []
    assert created["templates"] == {"created": 1, "updated": 0, "failed": 0}
    assert created["declarations"] == {"created": 1, "updated": 0, "failed": 0}
    assert created["attachments"] == {"created": 1, "updated": 0, "failed": 0}
    assert created["aliases"] == {"restored": 1, "failed": 0}
    assert created["records"] == {"restored": 1, "failed": 0}

    # the data landed: declaration, module, mount, record, and the alias resolves
    assert await tai42_app.states.get_declaration(state) is not None
    assert await tai42_app.states.get_template(module) is not None
    assert [m["template"] for m in await tai42_app.states.list_attachments(state)] == [module]
    view = await tai42_app.states.read(state, _SUBJECT)
    assert view is not None
    assert view.data == {"n": 1}
    aliased = await tai42_app.states.read(state, _SUBJECT.model_copy(update={"key": "old"}))
    assert aliased is not None
    assert aliased.data == {"n": 1}

    # a second import of the same payload UPDATES every entity in place
    updated = await import_states(scoped)
    assert updated["errors"] == []
    assert updated["templates"]["updated"] == 1
    assert updated["declarations"]["updated"] == 1
    assert updated["attachments"]["updated"] == 1


async def test_import_reports_refused_entity_and_lands_the_rest(run_token: str) -> None:
    token = run_token
    state = _state(token)
    good_module = f"modg-{token}"
    bad_module = f"modbad-{token}"
    payload: dict[str, Any] = {
        "version": 1,
        "templates": [
            {"kind": "state-template", "name": good_module, "schema": _MODULE_SCHEMA},
            # a flow-side key the platform module document forbids: refused, reported, skipped
            {"kind": "state-template", "name": bad_module, "schema": {"type": "object"}, "views": {}},
        ],
        "declarations": [
            {"name": state, "schema": _BASE_SCHEMA, "subject_kinds": ["thread"], "default_subject_kind": "thread"}
        ],
        "attachments": [],
        "aliases": [],
        "records": [
            {
                "state": state,
                "target_kind": "agent",
                "target_name": "a",
                "subject_kind": "thread",
                "subject_key": "t1",
                "data": {"n": 5},
            }
        ],
    }

    report = await import_states(payload)

    # the one bad module is reported and skipped; the good module and every other entity land
    assert report["templates"]["created"] == 1
    assert report["templates"]["failed"] == 1
    assert any(bad_module in err for err in report["errors"])
    assert report["declarations"] == {"created": 1, "updated": 0, "failed": 0}
    assert report["records"] == {"restored": 1, "failed": 0}

    assert await tai42_app.states.get_template(good_module) is not None
    assert await tai42_app.states.get_template(bad_module) is None
    view = await tai42_app.states.read(state, _SUBJECT)
    assert view is not None
    assert view.data == {"n": 5}
