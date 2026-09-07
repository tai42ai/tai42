"""A REAL Postgres exercise of the subject-keyed store: subject-column read/apply, the
``_trace`` stamp under a traced mount, the ``state_writes`` ledger row with the completed
origin + touched paths, subject fold/alias resolution, content search, and migrate.

This needs real Postgres semantics (jsonb ``@>``, ``clock_timestamp()``, row locks) — there
is no fake here. It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres. Without the opt-in the test SKIPS VISIBLY
with a clear reason (never a silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import LiteralString

import pytest
from tai42_contract.states.models import CompletedOrigin, StateSubject
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.states.db import STATES_COMPONENT
from tai42_skeleton.states.modules import compose_effective_schema, validate_module
from tai42_skeleton.states.service import _validate_document
from tai42_skeleton.states.store import PostgresStatesStore, make_cursor

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"

# The states baseline tables, verbatim structure from the chain's 0001_baseline.sql
# (idempotent ``IF NOT EXISTS`` so an already-migrated database is left untouched).
_SCHEMA_SQL: tuple[LiteralString, ...] = (
    "CREATE TABLE IF NOT EXISTS state_declarations (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',"
    " schema JSONB NOT NULL DEFAULT '{}'::jsonb, effective_schema JSONB NOT NULL DEFAULT '{}'::jsonb,"
    " subject_kinds JSONB NOT NULL DEFAULT '[]'::jsonb, default_subject_kind TEXT NOT NULL DEFAULT '',"
    " retention_days INTEGER, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS state_modules (name TEXT PRIMARY KEY, body JSONB NOT NULL DEFAULT '{}'::jsonb,"
    " shipped_hash TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS state_mounts (state TEXT NOT NULL, module TEXT NOT NULL,"
    " path JSONB NOT NULL DEFAULT '[]'::jsonb, parameters JSONB NOT NULL DEFAULT '{}'::jsonb,"
    " declarations JSONB NOT NULL DEFAULT '{}'::jsonb, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
    " PRIMARY KEY (state, module))",
    "CREATE TABLE IF NOT EXISTS state_records (state TEXT NOT NULL, target_kind TEXT NOT NULL,"
    " target_name TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_key TEXT NOT NULL,"
    " data JSONB NOT NULL DEFAULT '{}'::jsonb, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
    " PRIMARY KEY (state, target_kind, target_name, subject_kind, subject_key))",
    "CREATE INDEX IF NOT EXISTS state_records_data_gin ON state_records USING gin (data jsonb_path_ops)",
    "CREATE TABLE IF NOT EXISTS state_subject_aliases (state TEXT NOT NULL, target_kind TEXT NOT NULL,"
    " target_name TEXT NOT NULL, alias_kind TEXT NOT NULL, alias_key TEXT NOT NULL, canonical_kind TEXT NOT NULL,"
    " canonical_key TEXT NOT NULL, mode TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
    " PRIMARY KEY (state, target_kind, target_name, alias_kind, alias_key))",
    "CREATE TABLE IF NOT EXISTS state_applied_ops (op_id TEXT PRIMARY KEY,"
    " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS state_writes (id BIGSERIAL PRIMARY KEY, state TEXT NOT NULL, target_kind TEXT NOT NULL,"
    " target_name TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_key TEXT NOT NULL, seq DOUBLE PRECISION,"
    " at TIMESTAMPTZ NOT NULL DEFAULT now(), door TEXT NOT NULL, actor TEXT, consumer TEXT, meta JSONB, run_id TEXT,"
    " turn_id TEXT, paths JSONB NOT NULL DEFAULT '[]'::jsonb, op_id TEXT)",
)

_ORIGIN = CompletedOrigin(
    consumer="consumer-x",
    meta={"node": "node1"},
    run_id="run1",
    door="conversation",
    actor="user-1",
    turn_id="turn-1",
    inbound_id="inb-1",
)


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(STATES_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def real_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[PostgresStatesStore, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres states store test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs jsonb + row locking — no fake)"
        )
    reset_all_settings()
    for sql in _SCHEMA_SQL:
        await _exec(sql)
    state = f"st_{uuid.uuid4().hex[:12]}"
    yield PostgresStatesStore(), state
    # Every row this run wrote is scoped by the unique ``state``: state-columned tables
    # delete on ``state``; the two GLOBAL ledgers with no ``state`` column — the applied-op
    # ledger (op-ids suffixed with the unique ``state``) and the module table (named
    # ``state + "_m"``) — delete on that suffix/name, so a re-run starts clean.
    await _exec("DELETE FROM state_writes WHERE state = %s", (state,))
    await _exec("DELETE FROM state_subject_aliases WHERE state = %s", (state,))
    await _exec("DELETE FROM state_records WHERE state = %s", (state,))
    await _exec("DELETE FROM state_mounts WHERE state = %s", (state,))
    await _exec("DELETE FROM state_applied_ops WHERE op_id LIKE %s", (f"%:{state}",))
    await _exec("DELETE FROM state_modules WHERE name = %s", (state + "_m",))
    await _exec("DELETE FROM state_declarations WHERE name = %s", (state,))


async def test_apply_read_writes_and_trace(real_store: tuple[PostgresStatesStore, str]) -> None:
    store, state = real_store
    schema = {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"id": {"type": "integer"}, "_trace": {"type": "object"}},
                        },
                    }
                },
            }
        },
    }
    await store.upsert_declaration(state, "", schema, ["thread"], "thread", None, effective_schema=schema)
    await store.upsert_module(
        state + "_m",
        {"kind": "state-module", "name": state + "_m", "schema": {"type": "object"}, "trace": {"enabled": True}},
        None,
    )
    await store.upsert_mount(state, state + "_m", ["a"], {}, {}, effective_schema=schema)

    op_id = f"op-1:{state}"
    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
    applied, data, seq, skipped = await store.apply_ops(
        state,
        subject,
        [{"op": "set_by_key", "path": ["a", "items"], "key_field": "id", "value": {"id": 1}}],
        op_id=op_id,
        origin=_ORIGIN,
        validate_doc=_validate_document,
        retention_days=30,
    )
    assert applied
    assert seq is not None
    assert skipped == []
    assert data is not None
    assert data["a"]["items"][0]["_trace"]["meta"] == {"node": "node1"}

    read, _rseq = await store.read_record(state, subject)
    assert read is not None
    assert read["a"]["items"][0]["id"] == 1

    writes = await store.writes(state, subject, limit=10, cursor=None)
    assert len(writes) == 1
    assert writes[0]["door"] == "conversation"
    assert writes[0]["actor"] == "user-1"
    assert writes[0]["paths"] == [["a", "items"]]

    # a replayed op-id does not re-apply
    replay = await store.apply_ops(
        state, subject, [], op_id=op_id, origin=_ORIGIN, validate_doc=_validate_document, retention_days=30
    )
    assert replay[0] is False


async def test_traced_keyed_write_from_metaless_origin_stamps_null_meta(
    real_store: tuple[PostgresStatesStore, str],
) -> None:
    """A writer with no provenance bag (a hook/schedule/api door, or a builtin ``state_*``
    tool) carries ``meta=None``. The ``_trace`` stamp under a traced mount lands and reads
    back with ``_trace.meta`` null and ``_trace.at`` a string.

    The effective schema is the REAL composed one (``_inject_trace`` stamps ``_TRACE_SCHEMA``
    onto every object under the tracing mount), so validation runs against the schema the
    platform actually serves — which must admit a null meta object, not just a string."""
    store, state = real_store
    module_body = {
        "kind": "state-module",
        "name": "m",
        "schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "integer"}}},
                }
            },
        },
        "trace": {"enabled": True},
    }
    effective = compose_effective_schema(
        {"type": "object", "properties": {}}, [(validate_module(module_body), ["a"], {})]
    )
    # the composed schema carries the real injected trace schema on the array's items
    assert "_trace" in effective["properties"]["a"]["properties"]["items"]["items"]["properties"]
    await store.upsert_declaration(state, "", {}, ["thread"], "thread", None, effective_schema=effective)
    await store.upsert_module(state + "_m", {**module_body, "name": state + "_m"}, None)
    await store.upsert_mount(state, state + "_m", ["a"], {}, {}, effective_schema=effective)

    # the shape a builtin ``state_*`` tool produces (WriteOrigin meta=None), completed by
    # the hook door: meta/run/turn/inbound all null, only ``at`` and ``door`` stamped
    origin = CompletedOrigin(consumer="state_apply", meta=None, door="hook")
    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
    applied, data, seq, skipped = await store.apply_ops(
        state,
        subject,
        [{"op": "set_by_key", "path": ["a", "items"], "key_field": "id", "value": {"id": 1}}],
        op_id=f"op-1:{state}",
        origin=origin,
        validate_doc=_validate_document,
        retention_days=30,
    )
    assert applied
    assert seq is not None
    assert skipped == []
    assert data is not None
    trace = data["a"]["items"][0]["_trace"]
    assert trace["meta"] is None
    assert isinstance(trace["at"], str)

    read, _rseq = await store.read_record(state, subject)
    assert read is not None
    assert read["a"]["items"][0]["_trace"]["meta"] is None


async def test_composing_shape_refused(real_store: tuple[PostgresStatesStore, str]) -> None:
    from tai42_contract.states.errors import RegimeViolationError

    store, state = real_store
    schema = {"type": "object", "properties": {"a": {"type": "object", "properties": {"items": {"type": "array"}}}}}
    await store.upsert_declaration(state, "", schema, ["thread"], "thread", None, effective_schema=schema)
    await store.upsert_module(
        state + "_m",
        {
            "kind": "state-module",
            "name": state + "_m",
            "schema": {"type": "object"},
            "regimes": [{"path": ["items"], "regime": "composing"}],
        },
        None,
    )
    await store.upsert_mount(state, state + "_m", ["a"], {}, {}, effective_schema=schema)
    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
    with pytest.raises(RegimeViolationError):
        await store.apply_ops(
            state,
            subject,
            [{"op": "set", "path": ["a", "items"], "value": []}],
            op_id=None,
            origin=_ORIGIN,
            validate_doc=_validate_document,
            retention_days=30,
        )


async def test_fold_switch_lands_old_key_on_survivor_and_search(
    real_store: tuple[PostgresStatesStore, str],
) -> None:
    store, state = real_store
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await store.upsert_declaration(state, "", schema, ["thread"], "thread", None, effective_schema=schema)
    s1 = StateSubject(target_kind="agent", target_name="a", kind="thread", key="old")
    s2 = StateSubject(target_kind="agent", target_name="a", kind="thread", key="new")
    # the survivor holds its OWN record before the fold — ``switch`` keeps it and drops s1
    await store.replace(state, s2, {"n": 2}, origin=_ORIGIN, validate_doc=_validate_document)
    await store.replace(state, s1, {"n": 1}, origin=_ORIGIN, validate_doc=_validate_document)
    await store.fold_subject(state, s1, s2, "switch", origin=_ORIGIN, validate_doc=_validate_document)
    # a read via the OLD key lands on the surviving record (the survivor's data, its subject)
    view = await store.read_record_view(state, s1)
    assert view is not None
    assert view["canonical_subject"].key == "new"
    assert view["data"] == {"n": 2}
    read, _seq = await store.read_record(state, s1)
    assert read == {"n": 2}
    # content search finds the survivor under its own key …
    rows = await store.search_records(state, {"n": 2}, limit=10, cursor=None)
    assert any(r["subject_key"] == "new" for r in rows)
    # … and the DROPPED source record's data is gone entirely
    assert await store.search_records(state, {"n": 1}, limit=10, cursor=None) == []


async def test_fold_merge_moves_absent_members(real_store: tuple[PostgresStatesStore, str]) -> None:
    store, state = real_store
    schema = {"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "string"}}}
    await store.upsert_declaration(state, "", schema, ["thread"], "thread", None, effective_schema=schema)
    s1 = StateSubject(target_kind="agent", target_name="a", kind="thread", key="old")
    s2 = StateSubject(target_kind="agent", target_name="a", kind="thread", key="new")
    await store.replace(state, s1, {"x": "from-old", "y": "old-loses"}, origin=_ORIGIN, validate_doc=_validate_document)
    await store.replace(state, s2, {"y": "new-wins"}, origin=_ORIGIN, validate_doc=_validate_document)
    report = await store.fold_subject(state, s1, s2, "merge", origin=_ORIGIN, validate_doc=_validate_document)
    # only the member the survivor LACKED moves; the survivor wins the shared member
    assert report["merged_members"] == ["x"]
    merged = {"x": "from-old", "y": "new-wins"}
    read_new, _s = await store.read_record(state, s2)
    assert read_new == merged
    read_old, _s2 = await store.read_record(state, s1)  # old key resolves to the survivor
    assert read_old == merged


async def test_fold_switch_into_empty_survivor_leaves_no_record(
    real_store: tuple[PostgresStatesStore, str],
) -> None:
    store, state = real_store
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await store.upsert_declaration(state, "", schema, ["thread"], "thread", None, effective_schema=schema)
    s1 = StateSubject(target_kind="agent", target_name="a", kind="thread", key="old")
    s2 = StateSubject(target_kind="agent", target_name="a", kind="thread", key="new")
    await store.replace(state, s1, {"n": 1}, origin=_ORIGIN, validate_doc=_validate_document)
    # switch into a survivor that has NO record: s1's record is dropped and none is created
    await store.fold_subject(state, s1, s2, "switch", origin=_ORIGIN, validate_doc=_validate_document)
    assert (await store.read_record(state, s1))[0] is None
    assert (await store.read_record(state, s2))[0] is None
    assert await store.list_subjects(state, kind=None, limit=10, cursor=None) == []


async def test_list_and_search_page_over_full_subject_identity(
    real_store: tuple[PostgresStatesStore, str],
) -> None:
    store, state = real_store
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await store.upsert_declaration(state, "", schema, ["thread"], "thread", None, effective_schema=schema)
    # two DIFFERENT targets share ONE subject key — the boundary a (kind, key)-only keyset
    # dropped. With page size 1 every row must be walked exactly once.
    sa = StateSubject(target_kind="agent", target_name="a", kind="thread", key="same")
    sb = StateSubject(target_kind="agent", target_name="b", kind="thread", key="same")
    await store.replace(state, sa, {"n": 1}, origin=_ORIGIN, validate_doc=_validate_document)
    await store.replace(state, sb, {"n": 2}, origin=_ORIGIN, validate_doc=_validate_document)

    def _next(rows: list[dict]) -> str:
        last = rows[-1]
        return make_cursor(last["target_kind"], last["target_name"], last["subject_kind"], last["subject_key"])

    listed: list[tuple[str, str]] = []
    cursor: str | None = None
    while True:
        rows = await store.list_subjects(state, kind=None, limit=1, cursor=cursor)
        if not rows:
            break
        listed.extend((r["target_name"], r["subject_key"]) for r in rows)
        cursor = _next(rows)
    assert listed == [("a", "same"), ("b", "same")]

    searched: list[tuple[str, str]] = []
    cursor = None
    while True:
        rows = await store.search_records(state, {}, limit=1, cursor=cursor)
        if not rows:
            break
        searched.extend((r["target_name"], r["subject_key"]) for r in rows)
        cursor = _next(rows)
    assert searched == [("a", "same"), ("b", "same")]


async def test_migrate_drops_field(real_store: tuple[PostgresStatesStore, str]) -> None:
    store, state = real_store
    old = {"type": "object", "properties": {"n": {"type": "integer"}, "g": {"type": "string"}}}
    new = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await store.upsert_declaration(state, "", old, ["thread"], "thread", None, effective_schema=old)
    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")
    await store.replace(state, subject, {"n": 1, "g": "x"}, origin=_ORIGIN, validate_doc=_validate_document)

    async def convert(records):
        return {k: {"n": v["n"]} for k, v in records.items()}

    await store.migrate(state, new, new, decide=lambda _old: convert, origin=_ORIGIN)
    read, _seq = await store.read_record(state, subject)
    assert read == {"n": 1}
    # migrate records ONE write row per converted subject, whole-document paths ([[]])
    writes = await store.writes(state, subject, limit=10, cursor=None)
    assert writes[0]["paths"] == [[]]
    assert writes[0]["consumer"] == "consumer-x"
