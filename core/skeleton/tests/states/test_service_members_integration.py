"""A REAL Postgres exercise of the states service's ``template_jq`` programs through the
store: an update program applied through the ``apply`` chokepoint (a keyed op under a
composing regime, ``_trace`` stamped, one write ledger row with ``{"template_jq": ...}``
provenance), ``op_id`` idempotency, the composing-shape refusal a whole-path update write
earns, an input program that reads without writing, and the built-in reconciler closing an
orphan through a keyed op.

This needs real Postgres semantics (jsonb, row locks, the idempotency ledger) — there is no
fake here. It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*``
at a live Postgres. Without the opt-in the tests SKIP VISIBLY with a clear reason.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, LiteralString

import pytest
from tai42_contract.states.errors import RegimeViolationError
from tai42_contract.states.models import AttachBody, StateDeclaration, StateSubject, StateTemplateDocument, WriteOrigin
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.db import STATES_COMPONENT, states_entry
from tai42_skeleton.states.service import StatesService

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(STATES_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


def _module_body(name: str) -> dict[str, Any]:
    return {
        "kind": "state-template",
        "name": name,
        "schema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}
            },
        },
        "regimes": [{"path": ["items"], "regime": "composing"}],
        "trace": {"enabled": True},
        "template_jq": {
            "ids": {"purpose": "input", "jq": "[(.items // [])[] | .id]"},
            "count": {"purpose": "input", "jq": "(.items // []) | length"},
            "add": {
                "purpose": "update",
                "writes": [["items"]],
                "jq": '[{op: "set_by_key", path: ["items"], key_field: "id", value: .input}]',
            },
            "wipe": {"purpose": "update", "writes": [["items"]], "jq": '[{op: "set", path: ["items"], value: []}]'},
        },
    }


def _reconciler_body(name: str) -> dict[str, Any]:
    """A module that declares ``reconcile`` and a ``composing`` ``items`` path, closing an
    orphan with a KEYED op (``remove_by_key``) — the only close shape the composing regime
    admits (a whole-path ``set`` would be refused)."""
    return {
        "kind": "state-template",
        "name": name,
        "schema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}
            },
        },
        "regimes": [{"path": ["items"], "regime": "composing"}],
        "declarations": {"schema": {"type": "object", "properties": {"allowed": {"type": "array"}}}},
        "reconcile": {
            "view": (
                ".new.allowed as $a | [(.data.items // [])[] "
                "| select(.id as $i | ($a | index($i)) == null) | {id, label: (.id | tostring)}]"
            ),
            "resolutions": '["closed"]',
            "close": '[{op: "remove_by_key", path: ["items"], key_field: "id", key: .id}]',
        },
    }


async def _cleanup_state(state: str, module: str) -> None:
    await _exec("DELETE FROM state_writes WHERE state = %s", (state,))
    await _exec("DELETE FROM state_records WHERE state = %s", (state,))
    await _exec("DELETE FROM state_attachments WHERE state = %s", (state,))
    await _exec("DELETE FROM state_applied_ops WHERE op_id LIKE %s", (f"%:{state}",))
    await _exec("DELETE FROM state_templates WHERE name = %s", (module,))
    await _exec("DELETE FROM state_declarations WHERE name = %s", (state,))


@pytest.fixture
async def real_service(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[StatesService, str, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres states-service test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs jsonb + row locking — no fake)"
        )
    reset_all_settings()
    await apply_migrations([states_entry()])
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    state = f"st_{uuid.uuid4().hex[:12]}"
    module = f"mod{uuid.uuid4().hex[:10]}"
    svc = StatesService()
    await svc.put_declaration(
        StateDeclaration(
            name=state,
            schema={"type": "object", "properties": {"meta": {"type": "string"}}},
            subject_kinds=["thread"],
            default_subject_kind="thread",
        )
    )
    await svc.put_template(StateTemplateDocument.model_validate(_module_body(module)), replace=False)
    await svc.attach(state, module, AttachBody(path=["a"]))
    yield svc, state, module
    await _cleanup_state(state, module)


@pytest.fixture
async def real_reconciler(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[StatesService, str, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres reconcile test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs jsonb + row locking — no fake)"
        )
    reset_all_settings()
    await apply_migrations([states_entry()])
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    state = f"st_{uuid.uuid4().hex[:12]}"
    module = f"mod{uuid.uuid4().hex[:10]}"
    svc = StatesService()
    await svc.put_declaration(
        StateDeclaration(
            name=state,
            schema={"type": "object", "properties": {"meta": {"type": "string"}}},
            subject_kinds=["thread"],
            default_subject_kind="thread",
        )
    )
    await svc.put_template(StateTemplateDocument.model_validate(_reconciler_body(module)), replace=False)
    await svc.attach(state, module, AttachBody(path=["a"], declarations={"allowed": [1, 2, 3]}))
    yield svc, state, module
    await _cleanup_state(state, module)


def _subject(state: str) -> StateSubject:
    return StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")


async def test_update_program_applies_a_keyed_op_under_a_composing_regime_and_traces(
    real_service: tuple[StatesService, str, str],
) -> None:
    svc, state, _module = real_service
    subject = _subject(state)
    result = await svc.apply_template_jq(
        state, subject, "add", {"id": 1}, op_id=f"tjq-1:{state}", origin=WriteOrigin(meta={"template_jq": "add"})
    )
    assert result.name == "add"
    assert result.applied is True
    assert result.data is not None
    # The keyed op landed under the attachment path and was ``_trace``-stamped like any writer.
    item = result.data["a"]["items"][0]
    assert item["id"] == 1
    assert isinstance(item["_trace"]["at"], str)
    # The write ledger row carries the generic ``{"template_jq": ...}`` provenance.
    writes = await svc.writes(state, subject, limit=10, cursor=None)
    assert writes.items[0].origin.meta == {"template_jq": "add"}
    assert writes.items[0].origin.door == "api"
    # An input program reads the applied record back.
    view = await svc.eval_template_jq(state, subject, "ids", {})
    assert view.value == [1]


async def test_update_program_is_idempotent_on_a_replayed_op_id(
    real_service: tuple[StatesService, str, str],
) -> None:
    svc, state, _module = real_service
    subject = _subject(state)
    op_id = f"rule-2:{state}"
    first = await svc.apply_template_jq(state, subject, "add", {"id": 7}, op_id=op_id, origin=WriteOrigin())
    assert first.applied is True
    replay = await svc.apply_template_jq(state, subject, "add", {"id": 7}, op_id=op_id, origin=WriteOrigin())
    assert replay.applied is False
    # Only one write was recorded for the two calls.
    writes = await svc.writes(state, subject, limit=10, cursor=None)
    assert len(writes.items) == 1


async def test_update_program_whole_path_write_on_a_composing_path_is_refused(
    real_service: tuple[StatesService, str, str],
) -> None:
    svc, state, _module = real_service
    subject = _subject(state)
    await svc.apply_template_jq(state, subject, "add", {"id": 1}, op_id=f"tjq-3:{state}", origin=WriteOrigin())
    with pytest.raises(RegimeViolationError):
        await svc.apply_template_jq(state, subject, "wipe", None, op_id=f"tjq-4:{state}", origin=WriteOrigin())


async def test_input_program_reads_without_writing(real_service: tuple[StatesService, str, str]) -> None:
    svc, state, _module = real_service
    subject = _subject(state)
    await svc.apply_template_jq(state, subject, "add", {"id": 1}, op_id=f"tjq-5:{state}", origin=WriteOrigin())
    await svc.apply_template_jq(state, subject, "add", {"id": 2}, op_id=f"tjq-6:{state}", origin=WriteOrigin())
    result = await svc.eval_template_jq(state, subject, "count", {})
    assert result.value == 2
    # The input program recorded no extra write.
    writes = await svc.writes(state, subject, limit=10, cursor=None)
    assert len(writes.items) == 2


async def test_reconciler_closes_an_orphan_through_a_keyed_op(
    real_reconciler: tuple[StatesService, str, str],
) -> None:
    svc, state, module = real_reconciler
    subject = _subject(state)
    # Seed three items under the composing ``items`` path.
    await svc.replace(state, subject, {"a": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}, origin=WriteOrigin())
    # Narrowing ``allowed`` to [1, 3] orphans item 2; the built-in reconciler closes it with
    # the module's KEYED close op (remove_by_key) on the composing path — the write the
    # composing-shape guard admits (a whole-path ``set`` would be refused) — and the close
    # commits on the mount transaction together with the declarations edit.
    await svc.update_attachment_declarations(
        state, module, {"allowed": [1, 3]}, options={"orphans": "close", "resolution": "closed"}
    )
    view = await svc.read(state, subject)
    assert view is not None
    assert [item["id"] for item in view.data["a"]["items"]] == [1, 3]
    mounts = await svc.list_attachments(state, template=module)
    assert mounts[0]["declarations"] == {"allowed": [1, 3]}
