"""The tool-metadata backup export/import round-trip over an in-memory fake.

``export_tool_meta`` / ``import_tool_meta`` operate at the SQL layer (not the typed
store), so this suite drives them against ``FakeBackupPg`` — a two-table fake that
enforces the two constraints the restore must respect: the self-referential
``parent_id`` foreign key (a child can only be inserted once its parent exists) and
the ``tool_folders_parent_name_unique`` UNIQUE ``NULLS NOT DISTINCT (parent_id,
name)`` sibling rule (two folders sharing a ``name`` under DIFFERENT parents are
legal; two sharing ``(parent_id, name)`` — root names included — collide). Both are
raised as the real ``psycopg`` errors the restore transaction would surface.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.tool_meta.backup as backup_module
from tai42_skeleton.tool_meta.backup import export_tool_meta, import_tool_meta


class _Cursor:
    def __init__(self, pg: FakeBackupPg) -> None:
        self._pg = pg
        self._one: Any = None
        self._all: list[Any] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        norm = " ".join(sql.split())
        pg = self._pg
        self._one = None
        self._all = []

        if norm == "SELECT id, name, parent_id, created_at FROM tool_folders ORDER BY id":
            self._all = [
                (f["id"], f["name"], f["parent_id"], f["created_at"])
                for f in sorted(pg.folders.values(), key=lambda f: f["id"])
            ]
        elif norm == (
            "SELECT tool_name, display_name, folder_id, tags, hidden, badges, created_at "
            "FROM tool_meta ORDER BY tool_name"
        ):
            self._all = [
                (
                    m["tool_name"],
                    m["display_name"],
                    m["folder_id"],
                    list(m["tags"]),
                    m["hidden"],
                    list(m["badges"]),
                    m["created_at"],
                )
                for m in sorted(pg.meta.values(), key=lambda m: m["tool_name"])
            ]
        elif norm == "SELECT id FROM tool_folders":
            self._all = [(fid,) for fid in pg.folders]
        elif norm == "SELECT tool_name FROM tool_meta":
            self._all = [(name,) for name in pg.meta]
        elif norm.startswith("INSERT INTO tool_folders"):
            fid, name, parent_id, created_at = params
            if parent_id is not None and parent_id not in pg.folders:
                raise ForeignKeyViolation()  # self-FK: parent must already be present
            for other_id, other in pg.folders.items():
                if other_id != fid and other["parent_id"] == parent_id and other["name"] == name:
                    raise UniqueViolation()  # NULLS-NOT-DISTINCT (parent_id, name)
            pg.folders[fid] = {"id": fid, "name": name, "parent_id": parent_id, "created_at": created_at}
        elif norm.startswith("INSERT INTO tool_meta"):
            tool_name, display_name, folder_id, tags, hidden, badges, created_at = params
            if folder_id is not None and folder_id not in pg.folders:
                raise ForeignKeyViolation()
            pg.meta[tool_name] = {
                "tool_name": tool_name,
                "display_name": display_name,
                "folder_id": folder_id,
                "tags": list(tags),
                "hidden": hidden,
                "badges": list(badges),
                "created_at": created_at,
            }
        else:
            raise AssertionError(f"unhandled SQL in backup fake: {norm!r}")

    async def fetchall(self) -> list[Any]:
        return self._all

    async def fetchone(self) -> Any:
        return self._one


class _Txn:
    """Snapshot/restore savepoint: a raising statement rolls the whole restore
    transaction back, exactly as the real ``conn.transaction()`` would."""

    def __init__(self, pg: FakeBackupPg) -> None:
        self._pg = pg
        self._snapshot: tuple[dict, dict] | None = None

    async def __aenter__(self) -> _Txn:
        self._snapshot = (copy.deepcopy(self._pg.folders), copy.deepcopy(self._pg.meta))
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is not None and self._snapshot is not None:
            self._pg.folders, self._pg.meta = self._snapshot
        return False


class _Conn:
    def __init__(self, pg: FakeBackupPg) -> None:
        self._pg = pg

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def transaction(self) -> _Txn:
        return _Txn(self._pg)

    def cursor(self) -> _Cursor:
        return _Cursor(self._pg)


class _Pool:
    def __init__(self, pg: FakeBackupPg) -> None:
        self._pg = pg

    @asynccontextmanager
    async def connection(self):
        yield _Conn(self._pg)


class FakeBackupPg:
    """In-memory ``tool_folders`` + ``tool_meta`` the backup SQL runs against."""

    def __init__(self) -> None:
        self.folders: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, dict[str, Any]] = {}


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> FakeBackupPg:
    # The store resolves its bound database through the registry; a fake transport
    # models a configured deployment, so the default database must be on.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeBackupPg()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in backup fake: {client_cls!r}")
        yield _Pool(fake)

    monkeypatch.setattr(backup_module, "client_ctx", _ctx)
    return fake


async def test_round_trip_restores_same_name_folders_under_different_parents(pg: FakeBackupPg) -> None:
    # Two folders both named "Archive": one at the root, one nested under "Work".
    # They share a NAME but not a PARENT, so they are legal siblings-of-different-
    # parents — yet a NULL-first restore pass would collapse both to (NULL, "Archive")
    # and trip the NULLS-NOT-DISTINCT unique. The restore must keep the nesting intact.
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    pg.folders = {
        "work": {"id": "work", "name": "Work", "parent_id": None, "created_at": ts},
        "arch-root": {"id": "arch-root", "name": "Archive", "parent_id": None, "created_at": ts},
        "arch-nested": {"id": "arch-nested", "name": "Archive", "parent_id": "work", "created_at": ts},
    }
    pg.meta = {
        "weather": {
            "tool_name": "weather",
            "display_name": "Weather",
            "folder_id": "arch-nested",
            "tags": ["a"],
            "hidden": None,
            "badges": ["network", "storage-read"],
            "created_at": ts,
        }
    }

    payload = await export_tool_meta()

    # Wipe, then restore from the exported payload.
    pg.folders = {}
    pg.meta = {}
    report = await import_tool_meta(payload)

    assert report["created"] == 4  # three folders + the one overlay row, none lost to a collision
    assert not report["errors"]
    by_id = pg.folders
    assert len(by_id) == 3  # all three folders restored
    assert by_id["arch-root"]["parent_id"] is None
    assert by_id["arch-nested"]["parent_id"] == "work"  # nesting survived
    # Both "Archive" folders coexist under their distinct parents.
    assert {(f["name"], f["parent_id"]) for f in by_id.values()} == {
        ("Work", None),
        ("Archive", None),
        ("Archive", "work"),
    }
    assert pg.meta["weather"]["folder_id"] == "arch-nested"
    # The badge set survives the export/import round-trip verbatim.
    assert pg.meta["weather"]["badges"] == ["network", "storage-read"]


async def test_round_trip_restores_child_listed_before_parent(pg: FakeBackupPg) -> None:
    # A child appearing before its parent in the payload must still restore: the
    # not-deferrable self-FK requires the parent be inserted first, so the importer
    # orders topologically rather than trusting payload order.
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    payload = {
        "folders": [
            {"id": "child", "name": "child", "parent_id": "parent", "created_at": ts.isoformat()},
            {"id": "parent", "name": "parent", "parent_id": None, "created_at": ts.isoformat()},
        ],
        "rows": [],
    }
    report = await import_tool_meta(payload)
    assert report["created"] == 2
    assert pg.folders["child"]["parent_id"] == "parent"


async def test_skip_vs_overwrite_over_existing_rows(pg: FakeBackupPg) -> None:
    # A folder id + an overlay row that both already exist, plus one new folder id.
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    pg.folders = {"f1": {"id": "f1", "name": "Old", "parent_id": None, "created_at": ts}}
    pg.meta = {
        "weather": {
            "tool_name": "weather",
            "display_name": "Old label",
            "folder_id": "f1",
            "tags": [],
            "hidden": None,
            "badges": ["network"],
            "created_at": ts,
        }
    }
    payload = {
        "folders": [
            {"id": "f1", "name": "Renamed", "parent_id": None, "created_at": ts.isoformat()},
            {"id": "f2", "name": "Fresh", "parent_id": None, "created_at": ts.isoformat()},
        ],
        "rows": [
            {
                "tool_name": "weather",
                "display_name": "New label",
                "folder_id": "f1",
                "tags": ["x"],
                "hidden": None,
                "badges": ["storage-write"],
                "created_at": ts.isoformat(),
            }
        ],
    }

    # skip (the default): f2 is new -> created; f1 + weather already exist -> left as
    # they stand, counted as clean skips.
    skip = await import_tool_meta(payload)
    assert skip["created"] == 1
    assert skip["updated"] == 0
    assert skip["skipped_existing"] == 2
    assert pg.folders["f1"]["name"] == "Old"  # untouched
    assert pg.meta["weather"]["display_name"] == "Old label"  # untouched
    assert pg.folders["f2"]["name"] == "Fresh"  # the new folder landed

    # overwrite: every row now exists (the skip pass already created f2), so all three
    # are replaced in place.
    over = await import_tool_meta(payload, "overwrite")
    assert over["created"] == 0
    assert over["updated"] == 3
    assert over["skipped_existing"] == 0
    assert pg.folders["f1"]["name"] == "Renamed"
    assert pg.meta["weather"]["display_name"] == "New label"
    assert pg.meta["weather"]["badges"] == ["storage-write"]  # the overwrite replaced the badge set
