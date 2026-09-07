"""A stateful in-memory fake Postgres for the subject-keyed state store tests.

``FakeStatesPg`` models the seven tables :class:`~tai42_skeleton.states.store.
PostgresStatesStore` touches — ``state_declarations``, ``state_modules``, ``state_mounts``,
``state_records``, ``state_subject_aliases``, ``state_applied_ops`` (the idempotency
ledger) and ``state_writes`` (the write-provenance ledger) — and interprets the store's
EXACT SQL by normalized text, monkeypatched in over the pooled ``client_ctx`` so the REAL
store runs against it with no live database. It is faithful to the Postgres semantics the
store leans on:

* ``transaction()`` snapshots every table on enter and RESTORES them on an exception (a
  real rollback), so a partial-failure write leaves no orphan;
* a statement that RAISES inside a transaction ABORTS it exactly as Postgres does — every
  later statement on that connection fails with ``InFailedSqlTransaction`` until the block
  ends;
* ``clock_timestamp()``/``now()`` advance a monotone clock, so ``extract(epoch FROM
  updated_at)`` returns a strictly increasing ``seq`` — the channel ordering key;
* the record upsert-lock (``ON CONFLICT DO UPDATE SET state = EXCLUDED.state RETURNING …
  (xmax = 0) AS inserted``) reports ``inserted`` true only on a fresh row and leaves an
  existing row's ``updated_at`` untouched, exactly as the no-op update does;
* ``data @> %s::jsonb`` containment, keyset tuple comparison over the four subject columns,
  ``jsonb_object_keys`` field counts, ``ON CONFLICT DO NOTHING`` on the op ledger, and the
  ``make_interval`` retention predicates are all modeled.

The primary keys are enforced (a re-insert without ``ON CONFLICT`` raises a real
``UniqueViolation``), so the store's upsert clauses are exercised as written.
"""

from __future__ import annotations

import copy
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import InFailedSqlTransaction, UniqueViolation
from psycopg.types.json import Jsonb
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.states.store as store_module
from tai42_skeleton.states.store import PostgresStatesStore

_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)

_DECL_COLS = (
    "name",
    "description",
    "schema",
    "effective_schema",
    "subject_kinds",
    "default_subject_kind",
    "retention_days",
    "updated_at",
)


def _unwrap(value: Any) -> Any:
    """The stored Python value behind a psycopg ``Jsonb`` wrapper (the store wraps every
    jsonb parameter); a plain value passes through."""
    return value.obj if isinstance(value, Jsonb) else value


def _contains(data: Any, needle: Any) -> bool:
    """The ``@>`` jsonb containment predicate: every member of ``needle`` is present in
    ``data`` (recursively for objects; membership for arrays; equality for scalars)."""
    if isinstance(needle, dict):
        if not isinstance(data, dict):
            return False
        return all(k in data and _contains(data[k], v) for k, v in needle.items())
    if isinstance(needle, list):
        if not isinstance(data, list):
            return False
        return all(any(_contains(item, elem) for item in data) for elem in needle)
    return data == needle


class _FakeTxn:
    """Snapshot-and-restore savepoint: rolls every table back on any exception, and marks
    the connection's transaction block open/closed so an erroring statement inside it
    aborts the block (and a clean exit clears the abort)."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._pg = conn._pg
        self._snapshot: dict[str, Any] | None = None

    async def __aenter__(self) -> _FakeTxn:
        self._snapshot = self._pg.snapshot()
        self._conn.in_transaction = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is not None and self._snapshot is not None:
            self._pg.restore(self._snapshot)
        self._conn.in_transaction = False
        self._conn.aborted = False
        return False


class _FakeCursor:
    def __init__(self, conn: _FakeConn, *, row_factory: Any = None) -> None:
        self._conn = conn
        self._pg = conn._pg
        self.rowcount = 0
        self._one: Any = None
        self._all: list[Any] = []

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: Any = ()) -> None:
        if self._conn.aborted:
            raise InFailedSqlTransaction("current transaction is aborted, commands ignored until end of transaction")
        try:
            await self._dispatch(sql, params)
        except Exception:
            if self._conn.in_transaction:
                self._conn.aborted = True
            raise

    async def _dispatch(self, sql: str, params: Any) -> None:
        norm = " ".join(sql.split())
        pg = self._pg
        pg.executed.append((norm, params))
        self._one = None
        self._all = []
        self.rowcount = 0
        handler = _match(norm)
        if handler is None:
            raise AssertionError(f"unhandled SQL in fake: {norm!r}")
        handler(self, pg, norm, params)

    async def fetchone(self) -> Any:
        return self._one

    async def fetchall(self) -> list[Any]:
        return list(self._all)


class _FakeConn:
    def __init__(self, pg: FakeStatesPg) -> None:
        self._pg = pg
        self.in_transaction = False
        self.aborted = False

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def cursor(self, *, row_factory: Any = None) -> _FakeCursor:
        return _FakeCursor(self, row_factory=row_factory)

    def transaction(self) -> _FakeTxn:
        return _FakeTxn(self)


class _FakePool:
    def __init__(self, pg: FakeStatesPg) -> None:
        self._pg = pg

    @asynccontextmanager
    async def connection(self):
        yield _FakeConn(self._pg)


class FakeStatesPg:
    """In-memory stand-in for the seven state-store tables the store's SQL runs against."""

    def __init__(self) -> None:
        self.declarations: dict[str, dict[str, Any]] = {}  # name -> row
        self.modules: dict[str, dict[str, Any]] = {}  # name -> row
        self.mounts: dict[tuple[str, str], dict[str, Any]] = {}  # (state, module) -> row
        self.records: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.aliases: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self.applied_ops: dict[str, datetime] = {}  # op_id -> applied_at
        self.writes: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []
        self._clock = 0
        self._write_id = 0

    # -- clock + ids ---------------------------------------------------------
    def tick(self) -> datetime:
        self._clock += 1
        return _BASE_TIME + timedelta(seconds=self._clock)

    def now(self) -> datetime:
        return self.tick()

    def next_write_id(self) -> int:
        self._write_id += 1
        return self._write_id

    # -- snapshot / restore --------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "declarations": copy.deepcopy(self.declarations),
            "modules": copy.deepcopy(self.modules),
            "mounts": copy.deepcopy(self.mounts),
            "records": copy.deepcopy(self.records),
            "aliases": copy.deepcopy(self.aliases),
            "applied_ops": dict(self.applied_ops),
            "writes": copy.deepcopy(self.writes),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.declarations = snap["declarations"]
        self.modules = snap["modules"]
        self.mounts = snap["mounts"]
        self.records = snap["records"]
        self.aliases = snap["aliases"]
        self.applied_ops = snap["applied_ops"]
        self.writes = snap["writes"]

    # -- convenience seeding for tests --------------------------------------
    def seed_declaration(
        self,
        name: str,
        *,
        description: str = "",
        schema: dict[str, Any] | None = None,
        effective_schema: dict[str, Any] | None = None,
        subject_kinds: list[str] | None = None,
        default_subject_kind: str = "thread",
        retention_days: int | None = None,
    ) -> None:
        schema = schema if schema is not None else {"type": "object", "properties": {"n": {"type": "integer"}}}
        effective_schema = effective_schema if effective_schema is not None else schema
        subject_kinds = subject_kinds if subject_kinds is not None else ["thread"]
        self.declarations[name] = {
            "name": name,
            "description": description,
            "schema": schema,
            "effective_schema": effective_schema,
            "subject_kinds": list(subject_kinds),
            "default_subject_kind": default_subject_kind,
            "retention_days": retention_days,
            "updated_at": self.tick(),
        }

    def seed_record(
        self,
        state: str,
        target_kind: str,
        target_name: str,
        subject_kind: str,
        subject_key: str,
        data: dict[str, Any],
        *,
        updated_at: datetime | None = None,
    ) -> None:
        key = (state, target_kind, target_name, subject_kind, subject_key)
        self.records[key] = {
            "state": state,
            "target_kind": target_kind,
            "target_name": target_name,
            "subject_kind": subject_kind,
            "subject_key": subject_key,
            "data": data,
            "updated_at": updated_at if updated_at is not None else self.tick(),
        }


def _seq(dt: datetime) -> float:
    return dt.timestamp()


# --------------------------------------------------------------------------- #
# The SQL dispatch table: each entry matches a normalized statement and mutates #
# the in-memory tables / stages the row(s) the store then fetches.              #
# --------------------------------------------------------------------------- #
_HANDLERS: list[tuple[Any, Any]] = []


def _on(pattern: str):
    rx = re.compile(pattern)

    def register(fn):
        _HANDLERS.append((rx, fn))
        return fn

    return register


def _match(norm: str):
    for rx, fn in _HANDLERS:
        if rx.match(norm):
            return fn
    return None


# -- declarations ------------------------------------------------------------
@_on(
    r"SELECT name, description, schema, effective_schema, subject_kinds, default_subject_kind, retention_days, "
    r"updated_at FROM state_declarations WHERE name = %s$"
)
def _get_decl(cur, pg, norm, params):
    (name,) = params
    cur._one = pg.declarations.get(name)


@_on(
    r"SELECT name, description, schema, effective_schema, subject_kinds, default_subject_kind, retention_days, "
    r"updated_at FROM state_declarations ORDER BY name$"
)
def _list_decl(cur, pg, norm, params):
    cur._all = [pg.declarations[n] for n in sorted(pg.declarations)]


def _upsert_declaration(pg, params):
    (name, description, schema, effective, subject_kinds, default_subject_kind, retention_days) = params
    row = pg.declarations.get(name)
    values = {
        "name": name,
        "description": description,
        "schema": _unwrap(schema),
        "effective_schema": _unwrap(effective),
        "subject_kinds": list(_unwrap(subject_kinds)),
        "default_subject_kind": default_subject_kind,
        "retention_days": retention_days,
        "updated_at": pg.now(),
    }
    if row is None:
        pg.declarations[name] = values
    else:
        row.update(values)


@_on(
    r"INSERT INTO state_declarations \(name, description, schema, effective_schema, subject_kinds, "
    r"default_subject_kind, retention_days, updated_at\)"
)
def _insert_decl(cur, pg, norm, params):
    _upsert_declaration(pg, params)


@_on(r"SELECT schema, subject_kinds, default_subject_kind FROM state_declarations WHERE name = %s FOR UPDATE$")
def _lock_decl_for_guard(cur, pg, norm, params):
    (name,) = params
    cur._one = pg.declarations.get(name)


@_on(r"SELECT subject_kind, count\(\*\) AS n FROM state_records WHERE state = %s GROUP BY subject_kind$")
def _per_kind_counts(cur, pg, norm, params):
    (state,) = params
    counts: dict[str, int] = {}
    for rec in pg.records.values():
        if rec["state"] == state:
            counts[rec["subject_kind"]] = counts.get(rec["subject_kind"], 0) + 1
    cur._all = [{"subject_kind": k, "n": n} for k, n in counts.items()]


@_on(r"SELECT name FROM state_declarations WHERE name = %s FOR (UPDATE|SHARE)$")
def _lock_decl_name(cur, pg, norm, params):
    (name,) = params
    cur._one = {"name": name} if name in pg.declarations else None


@_on(r"SELECT effective_schema FROM state_declarations WHERE name = %s FOR (SHARE|UPDATE)$")
def _lock_effective_schema(cur, pg, norm, params):
    (name,) = params
    row = pg.declarations.get(name)
    cur._one = None if row is None else {"effective_schema": row["effective_schema"]}


@_on(
    r"SELECT m\.module, m\.path, mo\.body FROM state_mounts m JOIN state_modules mo ON mo\.name = m\.module "
    r"WHERE m\.state = %s$"
)
def _apply_mount_rows(cur, pg, norm, params):
    (state,) = params
    rows = []
    for (s, module), mount in pg.mounts.items():
        if s != state:
            continue
        mod = pg.modules.get(module)
        if mod is None:
            continue
        rows.append({"module": module, "path": mount["path"], "body": mod["body"]})
    rows.sort(key=lambda r: r["module"])
    cur._all = rows


@_on(r"DELETE FROM state_writes WHERE state = %s$")
def _del_writes(cur, pg, norm, params):
    (state,) = params
    before = len(pg.writes)
    pg.writes = [w for w in pg.writes if w["state"] != state]
    cur.rowcount = before - len(pg.writes)


@_on(r"DELETE FROM state_subject_aliases WHERE state = %s$")
def _del_aliases_state(cur, pg, norm, params):
    (state,) = params
    pg.aliases = {k: v for k, v in pg.aliases.items() if v["state"] != state}


@_on(r"DELETE FROM state_records WHERE state = %s$")
def _del_records_state(cur, pg, norm, params):
    (state,) = params
    pg.records = {k: v for k, v in pg.records.items() if v["state"] != state}


@_on(r"DELETE FROM state_mounts WHERE state = %s$")
def _del_mounts_state(cur, pg, norm, params):
    (state,) = params
    pg.mounts = {k: v for k, v in pg.mounts.items() if v["state"] != state}


@_on(r"DELETE FROM state_declarations WHERE name = %s$")
def _del_decl(cur, pg, norm, params):
    (name,) = params
    pg.declarations.pop(name, None)


@_on(r"SELECT count\(\*\) AS n FROM state_records WHERE state = %s$")
def _count_records(cur, pg, norm, params):
    (state,) = params
    cur._one = {"n": sum(1 for r in pg.records.values() if r["state"] == state)}


@_on(r"SELECT count\(\*\) AS n FROM state_records WHERE target_kind = %s AND target_name = %s$")
def _count_for_target(cur, pg, norm, params):
    tk, tn = params
    cur._one = {"n": sum(1 for r in pg.records.values() if r["target_kind"] == tk and r["target_name"] == tn)}


@_on(
    r"SELECT key, count\(\*\) AS n FROM state_records, jsonb_object_keys\(data\) AS key WHERE state = %s "
    r"GROUP BY key$"
)
def _field_stats_keys(cur, pg, norm, params):
    (state,) = params
    counts: dict[str, int] = {}
    for rec in pg.records.values():
        if rec["state"] != state:
            continue
        for key in rec["data"]:
            counts[key] = counts.get(key, 0) + 1
    cur._all = [{"key": k, "n": n} for k, n in counts.items()]


# -- modules -----------------------------------------------------------------
@_on(r"SELECT name, body, shipped_hash, updated_at FROM state_modules WHERE name = %s$")
def _get_module(cur, pg, norm, params):
    (name,) = params
    cur._one = pg.modules.get(name)


@_on(r"SELECT name, body, shipped_hash, updated_at FROM state_modules ORDER BY name$")
def _list_modules(cur, pg, norm, params):
    cur._all = [pg.modules[n] for n in sorted(pg.modules)]


@_on(r"SELECT module, count\(\*\) AS n FROM state_mounts GROUP BY module$")
def _mounted_counts(cur, pg, norm, params):
    counts: dict[str, int] = {}
    for _state, module in pg.mounts:
        counts[module] = counts.get(module, 0) + 1
    cur._all = [{"module": m, "n": n} for m, n in counts.items()]


@_on(r"INSERT INTO state_modules \(name, body, shipped_hash, updated_at\)")
def _upsert_module(cur, pg, norm, params):
    name, body, shipped_hash = params
    row = pg.modules.get(name)
    values = {"name": name, "body": _unwrap(body), "shipped_hash": shipped_hash, "updated_at": pg.now()}
    if row is None:
        pg.modules[name] = values
    else:
        row.update(values)


@_on(r"DELETE FROM state_modules WHERE name = %s$")
def _delete_module(cur, pg, norm, params):
    (name,) = params
    cur.rowcount = 1 if pg.modules.pop(name, None) is not None else 0


# -- mounts ------------------------------------------------------------------
_MOUNT_COLS = ("state", "module", "path", "parameters", "declarations", "updated_at")


@_on(
    r"SELECT state, module, path, parameters, declarations, updated_at FROM state_mounts "
    r"WHERE state = %s AND module = %s$"
)
def _get_mount(cur, pg, norm, params):
    state, module = params
    cur._one = pg.mounts.get((state, module))


@_on(
    r"SELECT state, module, path, parameters, declarations, updated_at FROM state_mounts WHERE state = %s "
    r"ORDER BY module$"
)
def _list_mounts_for_state(cur, pg, norm, params):
    (state,) = params
    rows = [v for (s, _m), v in pg.mounts.items() if s == state]
    cur._all = sorted(rows, key=lambda r: r["module"])


@_on(
    r"SELECT state, module, path, parameters, declarations, updated_at FROM state_mounts WHERE module = %s "
    r"ORDER BY state$"
)
def _list_mounts_of_module(cur, pg, norm, params):
    (module,) = params
    rows = [v for (_s, m), v in pg.mounts.items() if m == module]
    cur._all = sorted(rows, key=lambda r: r["state"])


@_on(
    r"SELECT state, module, path, parameters, declarations, updated_at FROM state_mounts "
    r"ORDER BY state, module$"
)
def _list_all_mounts(cur, pg, norm, params):
    cur._all = sorted(pg.mounts.values(), key=lambda r: (r["state"], r["module"]))


@_on(r"INSERT INTO state_mounts \(state, module, path, parameters, declarations, updated_at\)")
def _upsert_mount(cur, pg, norm, params):
    state, module, path, parameters, declarations = params
    key = (state, module)
    values = {
        "state": state,
        "module": module,
        "path": _unwrap(path),
        "parameters": _unwrap(parameters),
        "declarations": _unwrap(declarations),
        "updated_at": pg.now(),
    }
    if key in pg.mounts:
        pg.mounts[key].update(values)
    else:
        pg.mounts[key] = values


@_on(r"UPDATE state_mounts SET declarations = %s, updated_at = now\(\) WHERE state = %s AND module = %s$")
def _update_mount_declarations(cur, pg, norm, params):
    declarations, state, module = params
    row = pg.mounts.get((state, module))
    if row is not None:
        row["declarations"] = _unwrap(declarations)
        row["updated_at"] = pg.now()
        cur.rowcount = 1


@_on(r"UPDATE state_mounts SET parameters = %s, updated_at = now\(\) WHERE state = %s AND module = %s$")
def _update_mount_parameters(cur, pg, norm, params):
    parameters, state, module = params
    row = pg.mounts.get((state, module))
    if row is not None:
        row["parameters"] = _unwrap(parameters)
        row["updated_at"] = pg.now()
        cur.rowcount = 1


@_on(r"DELETE FROM state_mounts WHERE state = %s AND module = %s$")
def _delete_mount(cur, pg, norm, params):
    state, module = params
    cur.rowcount = 1 if pg.mounts.pop((state, module), None) is not None else 0


@_on(r"UPDATE state_declarations SET effective_schema = %s, updated_at = now\(\) WHERE name = %s$")
def _update_effective_schema(cur, pg, norm, params):
    effective, name = params
    row = pg.declarations.get(name)
    if row is not None:
        row["effective_schema"] = _unwrap(effective)
        row["updated_at"] = pg.now()
        cur.rowcount = 1


# -- alias resolution --------------------------------------------------------
@_on(
    r"SELECT canonical_kind, canonical_key FROM state_subject_aliases WHERE state = %s AND target_kind = %s "
    r"AND target_name = %s AND alias_kind = %s AND alias_key = %s$"
)
def _resolve_alias(cur, pg, norm, params):
    state, tk, tn, ak, akey = params
    row = pg.aliases.get((state, tk, tn, ak, akey))
    cur._one = (
        None
        if row is None
        else {
            "canonical_kind": row["canonical_kind"],
            "canonical_key": row["canonical_key"],
        }
    )


# -- record reads ------------------------------------------------------------
@_on(
    r"SELECT data, extract\(epoch FROM updated_at\)::float8 AS seq FROM state_records WHERE state = %s "
    r"AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s$"
)
def _read_record(cur, pg, norm, params):
    key = tuple(params)
    row = pg.records.get(key)
    cur._one = None if row is None else {"data": row["data"], "seq": _seq(row["updated_at"])}


@_on(
    r"SELECT alias_kind, alias_key FROM state_subject_aliases WHERE state = %s AND target_kind = %s "
    r"AND target_name = %s AND canonical_kind = %s AND canonical_key = %s ORDER BY alias_kind, alias_key$"
)
def _list_folded_from(cur, pg, norm, params):
    state, tk, tn, ck, ckey = params
    rows = [
        v
        for v in pg.aliases.values()
        if v["state"] == state
        and v["target_kind"] == tk
        and v["target_name"] == tn
        and v["canonical_kind"] == ck
        and v["canonical_key"] == ckey
    ]
    rows.sort(key=lambda r: (r["alias_kind"], r["alias_key"]))
    cur._all = [{"alias_kind": r["alias_kind"], "alias_key": r["alias_key"]} for r in rows]


@_on(
    r"SELECT target_kind, target_name, subject_kind, subject_key, data FROM state_records WHERE state = %s "
    r"ORDER BY target_kind, target_name, subject_kind, subject_key$"
)
def _export_records(cur, pg, norm, params):
    (state,) = params
    rows = [r for r in pg.records.values() if r["state"] == state]
    rows.sort(key=lambda r: (r["target_kind"], r["target_name"], r["subject_kind"], r["subject_key"]))
    cur._all = [
        {
            "target_kind": r["target_kind"],
            "target_name": r["target_name"],
            "subject_kind": r["subject_kind"],
            "subject_key": r["subject_key"],
            "data": r["data"],
        }
        for r in rows
    ]


@_on(
    r"SELECT target_kind, target_name, alias_kind, alias_key, canonical_kind, canonical_key, mode "
    r"FROM state_subject_aliases WHERE state = %s ORDER BY target_kind, target_name, alias_kind, alias_key$"
)
def _list_aliases(cur, pg, norm, params):
    (state,) = params
    rows = [v for v in pg.aliases.values() if v["state"] == state]
    rows.sort(key=lambda r: (r["target_kind"], r["target_name"], r["alias_kind"], r["alias_key"]))
    cur._all = [
        {
            k: r[k]
            for k in (
                "target_kind",
                "target_name",
                "alias_kind",
                "alias_key",
                "canonical_kind",
                "canonical_key",
                "mode",
            )
        }
        for r in rows
    ]


# -- write ledger ------------------------------------------------------------
@_on(
    r"INSERT INTO state_writes \(state, target_kind, target_name, subject_kind, subject_key, seq, at, door, "
    r"actor, consumer, meta, run_id, turn_id, paths, op_id\)"
)
def _insert_write(cur, pg, norm, params):
    (state, tk, tn, kind, key, seq, door, actor, consumer, meta, run_id, turn_id, paths, op_id) = params
    pg.writes.append(
        {
            "id": pg.next_write_id(),
            "state": state,
            "target_kind": tk,
            "target_name": tn,
            "subject_kind": kind,
            "subject_key": key,
            "seq": seq,
            "at": pg.now(),
            "door": door,
            "actor": actor,
            "consumer": consumer,
            "meta": _unwrap(meta) if meta is not None else None,
            "run_id": run_id,
            "turn_id": turn_id,
            "paths": _unwrap(paths),
            "op_id": op_id,
        }
    )


# -- record writes -----------------------------------------------------------
@_on(
    r"INSERT INTO state_records \(state, target_kind, target_name, subject_kind, subject_key, data, updated_at\) "
    r"VALUES \(%s, %s, %s, %s, %s, %s, clock_timestamp\(\)\) ON CONFLICT"
)
def _upsert_record_data(cur, pg, norm, params):
    # replace / restore / fold-merge: always writes data + a fresh updated_at.
    state, tk, tn, kind, key, data = params
    rkey = (state, tk, tn, kind, key)
    row = pg.records.get(rkey)
    stamp = pg.now()
    if row is None:
        pg.records[rkey] = {
            "state": state,
            "target_kind": tk,
            "target_name": tn,
            "subject_kind": kind,
            "subject_key": key,
            "data": _unwrap(data),
            "updated_at": stamp,
        }
    else:
        row["data"] = _unwrap(data)
        row["updated_at"] = stamp
    cur._one = {"seq": _seq(pg.records[rkey]["updated_at"])}


@_on(
    r"INSERT INTO state_records \(state, target_kind, target_name, subject_kind, subject_key, data, updated_at\) "
    r"VALUES \(%s, %s, %s, %s, %s, '\{\}'::jsonb, clock_timestamp\(\)\) ON CONFLICT"
)
def _upsert_record_lock(cur, pg, norm, params):
    # apply_ops upsert-lock: fresh row gets '{}' + new stamp (inserted=True); an existing
    # row is a no-op update (DO UPDATE SET state = EXCLUDED.state) keeping its updated_at.
    state, tk, tn, kind, key = params
    rkey = (state, tk, tn, kind, key)
    row = pg.records.get(rkey)
    if row is None:
        pg.records[rkey] = {
            "state": state,
            "target_kind": tk,
            "target_name": tn,
            "subject_kind": kind,
            "subject_key": key,
            "data": {},
            "updated_at": pg.now(),
        }
        row = pg.records[rkey]
        inserted = True
    else:
        inserted = False
    cur._one = {"data": row["data"], "seq": _seq(row["updated_at"]), "inserted": inserted}


@_on(
    r"UPDATE state_records SET data = %s, updated_at = clock_timestamp\(\) WHERE state = %s AND target_kind = %s "
    r"AND target_name = %s AND subject_kind = %s AND subject_key = %s RETURNING"
)
def _update_record_data(cur, pg, norm, params):
    data, state, tk, tn, kind, key = params
    rkey = (state, tk, tn, kind, key)
    row = pg.records.get(rkey)
    if row is not None:
        row["data"] = _unwrap(data)
        row["updated_at"] = pg.now()
        cur._one = {"seq": _seq(row["updated_at"])}
        cur.rowcount = 1


@_on(
    r"DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s "
    r"AND subject_key = %s$"
)
def _delete_record(cur, pg, norm, params):
    rkey = tuple(params)
    cur.rowcount = 1 if pg.records.pop(rkey, None) is not None else 0


# -- op ledger ---------------------------------------------------------------
@_on(r"INSERT INTO state_applied_ops \(op_id, applied_at\) VALUES \(%s, now\(\)\) ON CONFLICT DO NOTHING$")
def _insert_applied_op(cur, pg, norm, params):
    (op_id,) = params
    if op_id in pg.applied_ops:
        cur.rowcount = 0
    else:
        pg.applied_ops[op_id] = pg.now()
        cur.rowcount = 1


@_on(r"DELETE FROM state_applied_ops WHERE applied_at < now\(\) - make_interval\(days => %s\)$")
def _prune_applied_ops(cur, pg, norm, params):
    (days,) = params
    threshold = pg.now() - timedelta(days=days)
    victims = [op for op, at in pg.applied_ops.items() if at < threshold]
    for op in victims:
        del pg.applied_ops[op]
    cur.rowcount = len(victims)


# -- fold --------------------------------------------------------------------
@_on(
    r"SELECT data FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
    r"AND subject_kind = %s AND subject_key = %s$"
)
def _select_record_data(cur, pg, norm, params):
    rkey = tuple(params)
    row = pg.records.get(rkey)
    cur._one = None if row is None else {"data": row["data"]}


@_on(
    r"INSERT INTO state_subject_aliases \(state, target_kind, target_name, alias_kind, alias_key, canonical_kind, "
    r"canonical_key, mode\) VALUES \(%s, %s, %s, %s, %s, %s, %s, %s\)( ON CONFLICT)?"
)
def _insert_alias(cur, pg, norm, params):
    state, tk, tn, ak, akey, ck, ckey, mode = params
    key = (state, tk, tn, ak, akey)
    values = {
        "state": state,
        "target_kind": tk,
        "target_name": tn,
        "alias_kind": ak,
        "alias_key": akey,
        "canonical_kind": ck,
        "canonical_key": ckey,
        "mode": mode,
    }
    if "ON CONFLICT" in norm:
        pg.aliases[key] = values
        return
    if key in pg.aliases:
        raise UniqueViolation()
    pg.aliases[key] = values


@_on(
    r"UPDATE state_subject_aliases SET canonical_kind = %s, canonical_key = %s WHERE state = %s AND target_kind = %s "
    r"AND target_name = %s AND canonical_kind = %s AND canonical_key = %s$"
)
def _flatten_aliases(cur, pg, norm, params):
    new_ck, new_ckey, state, tk, tn, old_ck, old_ckey = params
    n = 0
    for v in pg.aliases.values():
        if (
            v["state"] == state
            and v["target_kind"] == tk
            and v["target_name"] == tn
            and v["canonical_kind"] == old_ck
            and v["canonical_key"] == old_ckey
        ):
            v["canonical_kind"] = new_ck
            v["canonical_key"] = new_ckey
            n += 1
    cur.rowcount = n


@_on(
    r"DELETE FROM state_subject_aliases WHERE state = %s AND target_kind = %s AND target_name = %s "
    r"AND canonical_kind = %s AND canonical_key = %s$"
)
def _delete_aliases_by_canonical(cur, pg, norm, params):
    state, tk, tn, ck, ckey = params
    victims = [
        k
        for k, v in pg.aliases.items()
        if v["state"] == state
        and v["target_kind"] == tk
        and v["target_name"] == tn
        and v["canonical_kind"] == ck
        and v["canonical_key"] == ckey
    ]
    for k in victims:
        del pg.aliases[k]
    cur.rowcount = len(victims)


# -- listing / search --------------------------------------------------------
def _subject_page(pg, state, after, limit, *, kind=None, containment=None):
    rows = [r for r in pg.records.values() if r["state"] == state]
    if kind is not None:
        rows = [r for r in rows if r["subject_kind"] == kind]
    if containment is not None:
        rows = [r for r in rows if _contains(r["data"], containment)]
    rows.sort(key=lambda r: (r["target_kind"], r["target_name"], r["subject_kind"], r["subject_key"]))
    out = []
    for r in rows:
        ident = (r["target_kind"], r["target_name"], r["subject_kind"], r["subject_key"])
        if ident > tuple(after):
            out.append(
                {
                    "target_kind": r["target_kind"],
                    "target_name": r["target_name"],
                    "subject_kind": r["subject_kind"],
                    "subject_key": r["subject_key"],
                    "updated_at": _seq(r["updated_at"]),
                }
            )
        if len(out) >= limit:
            break
    return out


@_on(
    r"SELECT target_kind, target_name, subject_kind, subject_key, extract\(epoch FROM updated_at\)::float8 "
    r"AS updated_at FROM state_records WHERE state = %s "
    r"AND \(target_kind, target_name, subject_kind, subject_key\) > \(%s, %s, %s, %s\) "
    r"ORDER BY target_kind, target_name, subject_kind, subject_key LIMIT %s$"
)
def _list_subjects_all(cur, pg, norm, params):
    state, t0, t1, t2, t3, limit = params
    cur._all = _subject_page(pg, state, (t0, t1, t2, t3), limit)


@_on(
    r"SELECT target_kind, target_name, subject_kind, subject_key, extract\(epoch FROM updated_at\)::float8 "
    r"AS updated_at FROM state_records WHERE state = %s "
    r"AND \(target_kind, target_name, subject_kind, subject_key\) > \(%s, %s, %s, %s\) "
    r"AND subject_kind = %s ORDER BY target_kind, target_name, subject_kind, subject_key LIMIT %s$"
)
def _list_subjects_kind(cur, pg, norm, params):
    state, t0, t1, t2, t3, kind, limit = params
    cur._all = _subject_page(pg, state, (t0, t1, t2, t3), limit, kind=kind)


@_on(
    r"SELECT target_kind, target_name, subject_kind, subject_key, extract\(epoch FROM updated_at\)::float8 "
    r"AS updated_at FROM state_records WHERE state = %s "
    r"AND \(target_kind, target_name, subject_kind, subject_key\) > \(%s, %s, %s, %s\) "
    r"AND data @> %s::jsonb ORDER BY target_kind, target_name, subject_kind, subject_key LIMIT %s$"
)
def _search_records(cur, pg, norm, params):
    state, t0, t1, t2, t3, containment, limit = params
    cur._all = _subject_page(pg, state, (t0, t1, t2, t3), limit, containment=_unwrap(containment))


# -- writes page -------------------------------------------------------------
def _writes_page(pg, state, tk, tn, kind, key, before, limit):
    rows = [
        w
        for w in pg.writes
        if w["state"] == state
        and w["target_kind"] == tk
        and w["target_name"] == tn
        and w["subject_kind"] == kind
        and w["subject_key"] == key
    ]
    if before is not None:
        rows = [w for w in rows if w["id"] < before]
    rows.sort(key=lambda w: w["id"], reverse=True)
    return [
        {
            k2: w[k2]
            for k2 in ("id", "seq", "at", "door", "actor", "consumer", "meta", "run_id", "turn_id", "paths", "op_id")
        }
        for w in rows[:limit]
    ]


@_on(
    r"SELECT id, seq, at, door, actor, consumer, meta, run_id, turn_id, paths, op_id FROM state_writes "
    r"WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s "
    r"ORDER BY id DESC LIMIT %s$"
)
def _writes_first(cur, pg, norm, params):
    state, tk, tn, kind, key, limit = params
    cur._all = _writes_page(pg, state, tk, tn, kind, key, None, limit)


@_on(
    r"SELECT id, seq, at, door, actor, consumer, meta, run_id, turn_id, paths, op_id FROM state_writes "
    r"WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s "
    r"AND id < %s ORDER BY id DESC LIMIT %s$"
)
def _writes_cursor(cur, pg, norm, params):
    state, tk, tn, kind, key, before, limit = params
    cur._all = _writes_page(pg, state, tk, tn, kind, key, before, limit)


# -- retention sweep ---------------------------------------------------------
@_on(
    r"DELETE FROM state_records r USING state_declarations d WHERE r.state = d.name "
    r"AND COALESCE\(d.retention_days, %\(default\)s\) IS NOT NULL "
    r"AND r.updated_at < now\(\) - make_interval\(days => COALESCE\(d.retention_days, %\(default\)s\)\) "
    r"RETURNING r.state$"
)
def _prune_expired(cur, pg, norm, params):
    default = params["default"]
    now = pg.now()
    victims = []
    for rkey, rec in pg.records.items():
        decl = pg.declarations.get(rec["state"])
        if decl is None:
            continue
        eff = decl["retention_days"] if decl["retention_days"] is not None else default
        if eff is None:
            continue
        if rec["updated_at"] < now - timedelta(days=eff):
            victims.append(rkey)
    for rkey in victims:
        state = pg.records[rkey]["state"]
        del pg.records[rkey]
        cur._all.append({"state": state})


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> FakeStatesPg:
    # The store resolves its bound database through the registry; a fake transport models a
    # configured deployment, so the default database must be on.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeStatesPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield _FakePool(fake)

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    return fake


@pytest.fixture
def store() -> PostgresStatesStore:
    return PostgresStatesStore()
