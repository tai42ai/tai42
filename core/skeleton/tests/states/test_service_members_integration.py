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
from collections.abc import AsyncIterator, Iterator
from typing import Any, LiteralString

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import RegimeViolationError, ValueValidationError
from tai42_contract.states.models import (
    AttachBody,
    StateBatchWrite,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    WriteOrigin,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.db import STATES_COMPONENT, states_entry
from tai42_skeleton.states.service import StatesService

from .fake_service_store import _FakeApp

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


@pytest.fixture(autouse=True)
def _bound_app() -> Iterator[None]:
    """A program body renders through ``tai42_app.storage.resource_manager`` before it is
    compiled or evaluated; bind the fake app so this real-Postgres exercise resolves the
    resource manager (inline ``content`` verbatim) without standing up the full app."""
    with tai42_app.bound(_FakeApp()):
        yield


@pytest.fixture(autouse=True)
async def _release_pools() -> AsyncIterator[None]:
    """Close this test's pooled Postgres clients at teardown.

    Each test runs on its own event loop and the store caches its pool per loop, so without an
    explicit close the pool's connections linger past the test — the whole real-Postgres suite
    would then accumulate open connections and exhaust the server's ``max_connections``. Set up
    first (autouse) so this teardown runs LAST, after the per-fixture cleanup opens and returns
    its own connections."""
    yield
    await shutdown_all_clients()


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(STATES_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


def _template_body(name: str) -> dict[str, Any]:
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
            "ids": {"purpose": "input", "jq": {"content": "[(.items // [])[] | .id]"}},
            "count": {"purpose": "input", "jq": {"content": "(.items // []) | length"}},
            "add": {
                "purpose": "update",
                "writes": [["items"]],
                "jq": {"content": '[{op: "set_by_key", path: ["items"], key_field: "id", value: $input}]'},
            },
            "wipe": {
                "purpose": "update",
                "writes": [["items"]],
                "jq": {"content": '[{op: "set", path: ["items"], value: []}]'},
            },
        },
    }


def _reconciler_body(name: str) -> dict[str, Any]:
    """A template that declares ``reconcile`` and a ``composing`` ``items`` path, closing an
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
            "orphans": {
                "content": (
                    "$new.allowed as $a | [(.items // [])[] "
                    "| select(.id as $i | ($a | index($i)) == null) | {id, label: (.id | tostring)}]"
                )
            },
            "resolutions": {"content": '["closed"]'},
            "close": {"content": '[{op: "remove_by_key", path: ["items"], key_field: "id", key: $id}]'},
        },
    }


async def _cleanup_state(state: str, template: str) -> None:
    await _exec("DELETE FROM state_writes WHERE state = %s", (state,))
    await _exec("DELETE FROM state_records WHERE state = %s", (state,))
    await _exec("DELETE FROM state_attachments WHERE state = %s", (state,))
    await _exec("DELETE FROM state_applied_ops WHERE op_id LIKE %s", (f"%:{state}",))
    await _exec("DELETE FROM state_templates WHERE name = %s", (template,))
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
    template = f"tpl{uuid.uuid4().hex[:10]}"
    svc = StatesService()
    await svc.put_declaration(
        StateDeclaration(
            name=state,
            schema={"type": "object", "properties": {"meta": {"type": "string"}}},
            subject_kinds=["thread"],
            default_subject_kind="thread",
        )
    )
    await svc.put_template(StateTemplateDocument.model_validate(_template_body(template)), replace=False)
    await svc.attach(state, template, AttachBody(path=["a"]))
    yield svc, state, template
    await _cleanup_state(state, template)


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
    template = f"tpl{uuid.uuid4().hex[:10]}"
    svc = StatesService()
    await svc.put_declaration(
        StateDeclaration(
            name=state,
            schema={"type": "object", "properties": {"meta": {"type": "string"}}},
            subject_kinds=["thread"],
            default_subject_kind="thread",
        )
    )
    await svc.put_template(StateTemplateDocument.model_validate(_reconciler_body(template)), replace=False)
    await svc.attach(state, template, AttachBody(path=["a"], declarations={"allowed": [1, 2, 3]}))
    yield svc, state, template
    await _cleanup_state(state, template)


def _subject(state: str) -> StateSubject:
    return StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")


async def test_update_program_applies_a_keyed_op_under_a_composing_regime_and_traces(
    real_service: tuple[StatesService, str, str],
) -> None:
    svc, state, _template = real_service
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
    svc, state, _template = real_service
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
    svc, state, _template = real_service
    subject = _subject(state)
    await svc.apply_template_jq(state, subject, "add", {"id": 1}, op_id=f"tjq-3:{state}", origin=WriteOrigin())
    with pytest.raises(RegimeViolationError):
        await svc.apply_template_jq(state, subject, "wipe", None, op_id=f"tjq-4:{state}", origin=WriteOrigin())


async def test_input_program_reads_without_writing(real_service: tuple[StatesService, str, str]) -> None:
    svc, state, _template = real_service
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
    svc, state, template = real_reconciler
    subject = _subject(state)
    # Seed three items under the composing ``items`` path.
    await svc.replace(state, subject, {"a": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}}, origin=WriteOrigin())
    # Narrowing ``allowed`` to [1, 3] orphans item 2; the built-in reconciler closes it with
    # the template's KEYED close op (remove_by_key) on the composing path — the write the
    # composing-shape guard admits (a whole-path ``set`` would be refused) — and the close
    # commits on the attach transaction together with the declarations edit.
    await svc.update_attachment_declarations(
        state, template, {"allowed": [1, 3]}, options={"orphans": "close", "resolution": "closed"}
    )
    view = await svc.read(state, subject)
    assert view is not None
    assert [item["id"] for item in view.data["a"]["items"]] == [1, 3]
    attachments = await svc.list_attachments(state, template=template)
    assert attachments[0]["declarations"] == {"allowed": [1, 3]}


def _plain_decl(state: str) -> StateDeclaration:
    return StateDeclaration(
        name=state,
        schema={"type": "object", "properties": {"n": {"type": "integer"}, "m": {"type": "integer"}}},
        subject_kinds=["thread"],
        default_subject_kind="thread",
    )


async def _cleanup_plain(state: str) -> None:
    await _exec("DELETE FROM state_writes WHERE state = %s", (state,))
    await _exec("DELETE FROM state_records WHERE state = %s", (state,))
    await _exec("DELETE FROM state_applied_ops WHERE op_id LIKE %s", (f"%:{state}",))
    await _exec("DELETE FROM state_declarations WHERE name = %s", (state,))


@pytest.fixture
async def real_plain(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[StatesService, str, str]]:
    """Two plain integer-field states (no template) for the ``apply_batch`` transaction exercises."""
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres apply_batch test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs jsonb + row locking — no fake)"
        )
    reset_all_settings()
    await apply_migrations([states_entry()])
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    state_a = f"st_{uuid.uuid4().hex[:12]}"
    state_b = f"st_{uuid.uuid4().hex[:12]}"
    svc = StatesService()
    await svc.put_declaration(_plain_decl(state_a))
    await svc.put_declaration(_plain_decl(state_b))
    yield svc, state_a, state_b
    await _cleanup_plain(state_a)
    await _cleanup_plain(state_b)


def _sub(key: str) -> StateSubject:
    return StateSubject(target_kind="agent", target_name="a", kind="thread", key=key)


async def test_apply_batch_commits_every_item_over_one_transaction(
    real_plain: tuple[StatesService, str, str],
) -> None:
    svc, state, _b = real_plain
    store = svc._store
    begins = 0
    original = store.begin

    def _counting_begin():
        nonlocal begins
        begins += 1
        return original()

    store.begin = _counting_begin  # type: ignore[method-assign]
    try:
        results = await svc.apply_batch(
            [
                StateBatchWrite(
                    state=state,
                    subject=_sub("t1"),
                    ops=[{"op": "set", "path": ["n"], "value": 1}],
                    origin=WriteOrigin(),
                ),
                StateBatchWrite(
                    state=state,
                    subject=_sub("t2"),
                    ops=[{"op": "set", "path": ["n"], "value": 2}],
                    origin=WriteOrigin(),
                ),
                StateBatchWrite(
                    state=state,
                    subject=_sub("t3"),
                    ops=[{"op": "set", "path": ["n"], "value": 3}],
                    origin=WriteOrigin(),
                ),
            ]
        )
    finally:
        store.begin = original  # type: ignore[method-assign]
    assert begins == 1  # ONE store.begin — one BEGIN/one COMMIT for the whole batch
    assert [r.applied for r in results] == [True, True, True]
    for key, value in (("t1", 1), ("t2", 2), ("t3", 3)):
        view = await svc.read(state, _sub(key))
        assert view is not None
        assert view.data == {"n": value}


async def test_apply_batch_rolls_the_whole_batch_back_when_a_later_item_raises(
    real_plain: tuple[StatesService, str, str],
) -> None:
    svc, state, _b = real_plain
    # The second item violates the effective schema (a string where an integer is declared),
    # so its document validation raises inside the shared transaction and the first item's
    # committed-looking write rolls back with it.
    with pytest.raises(ValueValidationError):
        await svc.apply_batch(
            [
                StateBatchWrite(
                    state=state,
                    subject=_sub("t1"),
                    ops=[{"op": "set", "path": ["n"], "value": 1}],
                    origin=WriteOrigin(),
                ),
                StateBatchWrite(
                    state=state,
                    subject=_sub("t2"),
                    ops=[{"op": "set", "path": ["n"], "value": "not-an-int"}],
                    origin=WriteOrigin(),
                ),
            ]
        )
    assert await svc.read(state, _sub("t1")) is None  # no partial write survived
    assert await svc.read(state, _sub("t2")) is None


async def test_apply_batch_is_idempotent_per_item_on_a_replayed_op_id(
    real_plain: tuple[StatesService, str, str],
) -> None:
    svc, state, _b = real_plain
    op_id = f"batch-op:{state}"
    await svc.apply(state, _sub("t1"), [{"op": "set", "path": ["n"], "value": 5}], op_id=op_id, origin=WriteOrigin())
    results = await svc.apply_batch(
        [
            StateBatchWrite(
                state=state,
                subject=_sub("t1"),
                ops=[{"op": "set", "path": ["n"], "value": 9}],
                op_id=op_id,
                origin=WriteOrigin(),
            ),
            StateBatchWrite(
                state=state, subject=_sub("t2"), ops=[{"op": "set", "path": ["n"], "value": 2}], origin=WriteOrigin()
            ),
        ]
    )
    assert results[0].applied is False  # the replayed op_id did not re-write
    assert results[1].applied is True  # its sibling landed
    first = await svc.read(state, _sub("t1"))
    assert first is not None
    assert first.data == {"n": 5}  # still the original value, not 9
    second = await svc.read(state, _sub("t2"))
    assert second is not None
    assert second.data == {"n": 2}


async def test_apply_batch_reads_your_writes_across_two_items_on_one_subject(
    real_plain: tuple[StatesService, str, str],
) -> None:
    svc, state, _b = real_plain
    # Item 2 carries a compare-and-set guard on ``n`` = 5; it applies ONLY if it observes
    # item 1's uncommitted write on the SAME subject over the shared transaction.
    results = await svc.apply_batch(
        [
            StateBatchWrite(
                state=state, subject=_sub("t1"), ops=[{"op": "set", "path": ["n"], "value": 5}], origin=WriteOrigin()
            ),
            StateBatchWrite(
                state=state,
                subject=_sub("t1"),
                ops=[{"op": "set", "path": ["m"], "value": 6, "guard": {"path": ["n"], "expected": 5}}],
                origin=WriteOrigin(),
            ),
        ]
    )
    assert results[1].applied is True
    assert results[1].skipped == []  # the guard passed — item 2 saw item 1's write
    view = await svc.read(state, _sub("t1"))
    assert view is not None
    assert view.data == {"n": 5, "m": 6}


async def test_apply_batch_spans_two_states_in_one_transaction(
    real_plain: tuple[StatesService, str, str],
) -> None:
    svc, state_a, state_b = real_plain
    results = await svc.apply_batch(
        [
            StateBatchWrite(
                state=state_a, subject=_sub("t1"), ops=[{"op": "set", "path": ["n"], "value": 1}], origin=WriteOrigin()
            ),
            StateBatchWrite(
                state=state_b, subject=_sub("t1"), ops=[{"op": "set", "path": ["n"], "value": 2}], origin=WriteOrigin()
            ),
        ]
    )
    assert [r.applied for r in results] == [True, True]
    view_a = await svc.read(state_a, _sub("t1"))
    view_b = await svc.read(state_b, _sub("t1"))
    assert view_a is not None
    assert view_a.data == {"n": 1}
    assert view_b is not None
    assert view_b.data == {"n": 2}


async def test_apply_batch_mixes_custom_ops_and_template_jq_items(
    real_service: tuple[StatesService, str, str],
) -> None:
    svc, state, _template = real_service
    # One raw-ops write (top-level ``meta``) and one ``template_jq`` update (``add`` under the
    # attachment path) land together in the one batch transaction, on two subjects.
    results = await svc.apply_batch(
        [
            StateBatchWrite(
                state=state,
                subject=_sub("t1"),
                ops=[{"op": "set", "path": ["meta"], "value": "hi"}],
                origin=WriteOrigin(),
            ),
            StateBatchWrite(
                state=state,
                subject=_sub("t2"),
                template_jq="add",
                input={"id": 1},
                origin=WriteOrigin(meta={"template_jq": "add"}),
            ),
        ]
    )
    assert [r.applied for r in results] == [True, True]
    custom = await svc.read(state, _sub("t1"))
    assert custom is not None
    assert custom.data["meta"] == "hi"
    templated = await svc.read(state, _sub("t2"))
    assert templated is not None
    assert templated.data["a"]["items"][0]["id"] == 1
