"""Store-level connector backup: categories + connections SQL round-trips.

Postgres is faked at the kit ``client_ctx`` seam (a stateful in-memory model of
the connector_category + connector_connections tables); no real database is
touched.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from psycopg.errors import UniqueViolation
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.store.backup as store_backup
from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.store.backup import (
    export_connector_categories,
    export_connector_connections,
    import_connector_categories,
    import_connector_connections,
)
from tai42_skeleton.connectors.store.redis_pg import _ALIAS_UNIQUE_CONSTRAINT, RedisPgConnectorTokenStore

from .conftest import CID, CID2

# Default creation time stamped on a fake row that was seeded without an explicit
# one, so the created_at-bearing export SELECTs always have a value to serialize.
_DEFAULT_CREATED_AT = datetime(2020, 1, 1, tzinfo=UTC)


class _AliasUniqueViolation(UniqueViolation):
    """A UniqueViolation carrying the alias constraint name, as psycopg raises it
    when the durable ``UNIQUE (provider_id, alias)`` is tripped."""

    diag: Any = SimpleNamespace(constraint_name=_ALIAS_UNIQUE_CONSTRAINT)


class _OtherUniqueViolation(UniqueViolation):
    """A UniqueViolation for a constraint OTHER than the alias uniqueness — the
    importer must re-raise this loudly rather than swallow it per-row."""

    diag: Any = SimpleNamespace(constraint_name="connector_connections_pkey")


# -- Stateful fake Postgres modelling the connector tables -------------------


class _FakeTxn:
    """A savepoint stand-in: propagates any exception (like psycopg's transaction
    context, which rolls back to the savepoint and re-raises)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeCursor:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg
        self._result_all: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        self._pg.executed.append(norm)
        self._result_all = []
        pg = self._pg
        if norm.startswith("SELECT id, display_name, sort_order"):
            self._result_all = [
                (c["id"], c["display_name"], c["sort_order"], c.get("created_at", _DEFAULT_CREATED_AT))
                for c in sorted(pg.categories.values(), key=lambda c: (c["sort_order"], c["id"]))
            ]
        elif norm == "SELECT id FROM connector_category":
            self._result_all = [(k,) for k in pg.categories]
        elif norm.startswith("INSERT INTO connector_category"):
            cid, display_name, sort_order, created_at = params
            pg.categories[cid] = {
                "id": cid,
                "display_name": display_name,
                "sort_order": sort_order,
                "created_at": created_at,
            }
        elif norm.startswith("SELECT connection_id, provider_id, alias, encrypted_blob"):
            self._result_all = [
                (uuid.UUID(k), r["provider_id"], r["alias"], r["blob"], r["exp"])
                for k, r in sorted(pg.connections.items())
            ]
        elif norm == "SELECT connection_id FROM connector_connections":
            self._result_all = [(uuid.UUID(k),) for k in pg.connections]
        elif norm.startswith("INSERT INTO connector_connections"):
            if pg.raise_on_conn_insert is not None:
                raise pg.raise_on_conn_insert
            conn_uuid, provider_id, alias, blob, exp = params
            cid = str(conn_uuid)
            for other_cid, r in pg.connections.items():
                if other_cid != cid and r["provider_id"] == provider_id and r["alias"] == alias:
                    raise _AliasUniqueViolation()
            pg.connections[cid] = {"provider_id": provider_id, "alias": alias, "blob": bytes(blob), "exp": exp}
        else:
            raise AssertionError(f"unhandled SQL in fake: {norm!r}")

    async def fetchall(self):
        return self._result_all


class _FakeConn:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._pg)

    def transaction(self):
        return _FakeTxn()


class _FakeRedis:
    """Minimal stand-in for the pooled Redis client the cache-invalidation path
    reaches through ``client_ctx(RedisClient, ...)``.

    ``warm`` models keys currently present in the cache; ``delete`` records every
    key it was asked to drop (so a test can assert the invalidation happened) and
    discards it from ``warm``, mirroring redis ``DEL`` returning the drop count."""

    def __init__(self) -> None:
        self.warm: set[str] = set()
        self.deleted: list[str] = []

    async def delete(self, *keys: str) -> int:
        dropped = 0
        for key in keys:
            self.deleted.append(key)
            if key in self.warm:
                self.warm.discard(key)
                dropped += 1
        return dropped


class _FakePg:
    def __init__(self) -> None:
        self.categories: dict[str, dict] = {}
        self.connections: dict[str, dict] = {}
        self.executed: list[str] = []
        self.redis = _FakeRedis()
        # When set to an exception, the next connector_connections INSERT raises
        # it — used to drive the non-alias UniqueViolation re-raise path.
        self.raise_on_conn_insert: Any = None

    def connection(self):
        return _FakeConn(self)


@pytest.fixture
def pg(monkeypatch):
    fake = _FakePg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is PostgresClient:
            yield fake
        elif client_cls is RedisClient:
            yield fake.redis
        else:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")

    monkeypatch.setattr(store_backup, "client_ctx", fake_client_ctx)
    return fake


def _wipe(pg: _FakePg) -> None:
    """Empty every table in place (simulate a fresh target) while keeping the
    fixture's ``client_ctx`` binding pointed at the same fake."""
    pg.categories.clear()
    pg.connections.clear()


# -- connector_category ------------------------------------------------------


async def test_categories_round_trip(pg):
    pg.categories["data"] = {"id": "data", "display_name": "Data", "sort_order": 4}
    pg.categories["other"] = {"id": "other", "display_name": "Other", "sort_order": 1000}

    payload = await export_connector_categories()
    assert {c["id"] for c in payload["categories"]} == {"data", "other"}

    _wipe(pg)
    report = await import_connector_categories(payload)
    assert report == {"created": 2, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    assert pg.categories["data"]["display_name"] == "Data"
    assert pg.categories["other"]["sort_order"] == 1000


async def test_categories_reimport_is_idempotent_updates(pg):
    pg.categories["data"] = {"id": "data", "display_name": "Data", "sort_order": 4}
    payload = await export_connector_categories()

    _wipe(pg)
    first = await import_connector_categories(payload)
    assert first == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}

    # Re-import over the now-populated table under overwrite: the row is an update.
    second = await import_connector_categories(payload, "overwrite")
    assert second == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}

    # Re-import over the same table under skip (the default): the row is left as it
    # stands, counted as a clean skip rather than an update.
    third = await import_connector_categories(payload)
    assert third == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": []}


async def test_categories_created_at_preserved_on_restore(pg):
    """The original ``created_at`` survives a restore into a fresh table — it is
    not reset to the restore time."""
    seeded = datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC)
    pg.categories["data"] = {"id": "data", "display_name": "Data", "sort_order": 1, "created_at": seeded}
    payload = await export_connector_categories()

    _wipe(pg)
    await import_connector_categories(payload)

    assert pg.categories["data"]["created_at"] == seeded


# -- connector_connections ---------------------------------------------------


def _seed_encrypted(pg: _FakePg, *, connection_id: str, provider_id: str, alias: str, plaintext: bytes) -> bytes:
    blob = crypto.encrypt(plaintext, connection_id=connection_id)
    pg.connections[connection_id] = {"provider_id": provider_id, "alias": alias, "blob": blob, "exp": None}
    return blob


async def test_connections_round_trip_blob_identical(pg):
    exp = datetime(2030, 6, 1, tzinfo=UTC)
    blob = crypto.encrypt(b"secret-token", connection_id=CID)
    pg.connections[CID] = {"provider_id": "acme", "alias": "work", "blob": blob, "exp": exp}

    payload = await export_connector_connections()
    assert len(payload) == 1
    entry = payload[0]
    assert entry["connection_id"] == CID
    assert entry["provider_id"] == "acme"
    assert entry["session_expires_at"] == exp.isoformat()
    # Ciphertext is base64 of the exact stored bytes — never decrypted.
    assert base64.b64decode(entry["encrypted_blob_b64"]) == blob

    # Wipe and restore.
    _wipe(pg)
    report = await import_connector_connections(payload)
    assert report == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}

    restored = pg.connections[CID]
    assert restored["blob"] == blob  # byte-for-byte identical
    assert restored["provider_id"] == "acme"
    assert restored["exp"] == exp


async def test_restored_blob_undecryptable_under_wrong_kek_raises(pg, monkeypatch):
    """A restore under a DIFFERENT CONNECTORS_KEK yields intact-but-dead
    ciphertext: decrypt fails LOUDLY (InvalidTag), never a silent skip."""
    blob = crypto.encrypt(b"secret-token", connection_id=CID)
    pg.connections[CID] = {"provider_id": "acme", "alias": "work", "blob": blob, "exp": None}

    payload = await export_connector_connections()
    _wipe(pg)
    await import_connector_connections(payload)
    restored = pg.connections[CID]["blob"]

    # Same KEK still decrypts the restored ciphertext.
    assert crypto.decrypt(restored, connection_id=CID) == b"secret-token"

    # Swap in a different 32-byte KEK; the restored ciphertext no longer decrypts.
    other_kek = base64.b64encode(bytes(range(100, 132))).decode("ascii")
    monkeypatch.setenv("CONNECTORS_KEK", other_kek)
    reset_all_settings()
    with pytest.raises(InvalidTag):
        crypto.decrypt(restored, connection_id=CID)


async def test_connections_alias_collision_on_different_id_is_reported(pg):
    """Importing a connection whose (provider_id, alias) is already held by a
    DIFFERENT connection_id trips the durable UNIQUE constraint, reported as a
    per-row error — never silent — while other rows still restore."""
    # Target already holds the alias under CID.
    _seed_encrypted(pg, connection_id=CID, provider_id="acme", alias="work", plaintext=b"a")

    # The backup carries a colliding row under CID2 plus a clean row (CID3).
    cid3 = "33333333-3333-4333-8333-333333333333"
    payload = [
        {
            "connection_id": CID2,
            "provider_id": "acme",
            "alias": "work",  # collides with CID's alias
            "session_expires_at": None,
            "encrypted_blob_b64": base64.b64encode(b"b").decode("ascii"),
        },
        {
            "connection_id": cid3,
            "provider_id": "acme",
            "alias": "home",  # clean
            "session_expires_at": None,
            "encrypted_blob_b64": base64.b64encode(b"c").decode("ascii"),
        },
    ]

    report = await import_connector_connections(payload)
    assert report["created"] == 1  # the clean row restored
    assert report["skipped"] == 1  # the colliding row rejected
    assert len(report["errors"]) == 1
    assert "already in use" in report["errors"][0]
    assert CID2 in report["errors"][0]
    # The clean row landed; the colliding one did not.
    assert cid3 in pg.connections
    assert CID2 not in pg.connections


async def test_connections_reimport_counts_updates(pg):
    blob = crypto.encrypt(b"tok", connection_id=CID)
    pg.connections[CID] = {"provider_id": "acme", "alias": "work", "blob": blob, "exp": None}
    payload = await export_connector_connections()

    # Re-import over the existing row under overwrite: it's an update, not a create.
    report = await import_connector_connections(payload, "overwrite")
    assert report == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}


async def test_connections_reimport_under_skip_leaves_existing(pg):
    """Under ``skip`` (the default) an already-present connection is left untouched —
    counted as a clean skip, never re-upserted."""
    blob = crypto.encrypt(b"tok", connection_id=CID)
    pg.connections[CID] = {"provider_id": "acme", "alias": "work", "blob": blob, "exp": None}
    payload = await export_connector_connections()

    report = await import_connector_connections(payload)
    assert report == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": []}


async def test_import_invalidates_warm_cache(pg):
    """A restore into a running deployment must DROP each restored connection's
    Redis cache key so the next ``get`` repopulates the restored token from
    Postgres — ``get`` serves a cached blob on a HIT with no read-side version
    check, so a warm entry would otherwise keep serving the stale pre-import token."""
    blob = crypto.encrypt(b"fresh-token", connection_id=CID)
    pg.connections[CID] = {"provider_id": "acme", "alias": "work", "blob": blob, "exp": None}
    payload = await export_connector_connections()

    rec_key = RedisPgConnectorTokenStore()._rec_key(CID)
    pg.redis.warm.add(rec_key)  # the connection is warm in the cache before the restore

    # Overwrite replaces the stored ciphertext, so the stale cache entry must go.
    await import_connector_connections(payload, "overwrite")

    assert rec_key in pg.redis.deleted  # the cache key was dropped
    assert rec_key not in pg.redis.warm  # so the next get repopulates from Postgres


async def test_skip_leaves_warm_cache_untouched(pg):
    """Under ``skip`` an existing connection is not re-upserted, so its warm cache
    entry — serving the connection that is being left in place — is NOT dropped."""
    blob = crypto.encrypt(b"fresh-token", connection_id=CID)
    pg.connections[CID] = {"provider_id": "acme", "alias": "work", "blob": blob, "exp": None}
    payload = await export_connector_connections()

    rec_key = RedisPgConnectorTokenStore()._rec_key(CID)
    pg.redis.warm.add(rec_key)

    await import_connector_connections(payload)  # skip is the default

    assert rec_key not in pg.redis.deleted
    assert rec_key in pg.redis.warm


async def test_import_invalidates_cache_on_canonical_id(pg):
    """A backup carrying a non-canonical connection_id (uppercase) still drops the
    CANONICAL cache key get() reads — invalidation keys on str(UUID), not the raw
    backup string."""
    payload = [
        {
            "connection_id": CID.upper(),
            "provider_id": "acme",
            "alias": "work",
            "session_expires_at": None,
            "encrypted_blob_b64": base64.b64encode(b"z").decode("ascii"),
        }
    ]
    canonical_key = RedisPgConnectorTokenStore()._rec_key(CID)  # CID is already canonical
    pg.redis.warm.add(canonical_key)

    await import_connector_connections(payload)

    assert canonical_key in pg.redis.deleted


async def test_connections_non_alias_unique_violation_raises(pg):
    """A UniqueViolation whose constraint is NOT the alias uniqueness must
    RE-RAISE loudly, never be swallowed into per-row errors (which would hide an
    unrelated durable-constraint failure as a benign skip)."""
    payload = [
        {
            "connection_id": CID,
            "provider_id": "acme",
            "alias": "work",
            "session_expires_at": None,
            "encrypted_blob_b64": base64.b64encode(b"x").decode("ascii"),
        }
    ]
    pg.raise_on_conn_insert = _OtherUniqueViolation()
    with pytest.raises(UniqueViolation):
        await import_connector_connections(payload)
