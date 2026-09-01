"""A stateful fake Postgres for the runs-index store tests.

Mirrors the versioned-document store test pattern: an in-memory stand-in that models
the single ``run_index`` table and interprets the store's SQL by normalized prefix,
monkeypatched over the pooled ``client_ctx``. It is faithful to the Postgres semantics
the store leans on:

* the ``run_id`` PRIMARY KEY rejects a duplicate INSERT (``UniqueViolation``);
* ``UPDATE ... trace_id = COALESCE(%s, trace_id)`` fills only a NULL trace id;
* ``UPDATE ... interaction_id = COALESCE(interaction_id, %s)`` is first-set-wins —
  a park id at the terminal write never clobbers a resume origin captured at START;
* the static optional-filter list reads the params in the store's fixed order and
  pages newest-first with a ``run_id`` tiebreak.

Timestamps written as ISO strings are parsed to ``datetime`` so range filters, order,
and the prune cutoff compare as the real ``timestamptz`` column would; reads yield
``datetime`` back, which the store's ``_iso`` renders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest
from psycopg.errors import UniqueViolation
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.runs.store as store_module


def _dt(value: Any) -> Any:
    """Parse an ISO string param to ``datetime`` (a ``datetime`` param passes through);
    the fake stores timestamps as ``datetime`` so ranges/order match Postgres."""
    return datetime.fromisoformat(value) if isinstance(value, str) else value


class _FakeCursor:
    def __init__(self, pg: FakeRunIndexPg) -> None:
        self._pg = pg
        self.rowcount = 0
        self._all: list = []

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, sql: str, params: tuple = ()) -> None:
        norm = " ".join(sql.split())
        pg = self._pg
        pg.executed.append(norm)
        self._all = []
        self.rowcount = 0

        if norm.startswith("INSERT INTO run_index"):
            run_id, preset_name, preset_version, trace_id, user_id, session_id, interaction_id, started_at = params
            if any(r["run_id"] == run_id for r in pg.rows):
                raise UniqueViolation("duplicate run_id")
            pg.rows.append(
                {
                    "run_id": run_id,
                    "preset_name": preset_name,
                    "preset_version": preset_version,
                    "trace_id": trace_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "interaction_id": interaction_id,
                    "outcome": "running",
                    "started_at": _dt(started_at),
                    "ended_at": None,
                }
            )
        elif norm.startswith("UPDATE run_index SET"):
            outcome, ended_at, trace_id, interaction_id, run_id = params
            for r in pg.rows:
                if r["run_id"] == run_id:
                    r["outcome"] = outcome
                    r["ended_at"] = _dt(ended_at)
                    if trace_id is not None:  # COALESCE(%s, trace_id) — a set param wins, NULL keeps
                        r["trace_id"] = trace_id
                    if r["interaction_id"] is None:  # COALESCE(interaction_id, %s) — first-set wins
                        r["interaction_id"] = interaction_id
                    self.rowcount = 1
        elif norm.startswith("SELECT run_id, preset_name, preset_version"):
            preset = params[0]
            version = params[2]
            user = params[4]
            session = params[6]
            interaction = params[8]
            outcome = params[10]
            t0 = params[12]
            t1 = params[14]
            limit = params[16]
            offset = params[17]
            matched = [
                r
                for r in pg.rows
                if (preset is None or r["preset_name"] == preset)
                and (version is None or r["preset_version"] == version)
                and (user is None or r["user_id"] == user)
                and (session is None or r["session_id"] == session)
                and (interaction is None or r["interaction_id"] == interaction)
                and (outcome is None or r["outcome"] == outcome)
                and (t0 is None or r["started_at"] >= t0)
                and (t1 is None or r["started_at"] <= t1)
            ]
            matched.sort(key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
            window = matched[offset : offset + limit]
            self._all = [
                (
                    r["run_id"],
                    r["preset_name"],
                    r["preset_version"],
                    r["trace_id"],
                    r["user_id"],
                    r["session_id"],
                    r["interaction_id"],
                    r["outcome"],
                    r["started_at"],
                    r["ended_at"],
                )
                for r in window
            ]
        elif norm.startswith("DELETE FROM run_index WHERE started_at <"):
            (cutoff,) = params
            keep = [r for r in pg.rows if not (r["started_at"] < cutoff)]
            self.rowcount = len(pg.rows) - len(keep)
            pg.rows = keep
        else:
            raise AssertionError(f"unhandled SQL in fake: {norm!r}")

    async def fetchall(self) -> list:
        return self._all


class _FakeConn:
    def __init__(self, pg: FakeRunIndexPg) -> None:
        self._pg = pg

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._pg)


class FakeRunIndexPg:
    """In-memory stand-in for the single ``run_index`` table."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.executed: list[str] = []

    def connection(self) -> _FakeConn:
        return _FakeConn(self)


@pytest.fixture
def pg(monkeypatch) -> FakeRunIndexPg:
    # The store resolves its bound database through the registry; the default database
    # must be on for ``component_store_settings`` to resolve.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeRunIndexPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    return fake
