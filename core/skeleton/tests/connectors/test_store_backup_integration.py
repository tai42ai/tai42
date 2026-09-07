"""A REAL Postgres + Redis round-trip of the connector backup sections: the encrypted
connection records restored under their original UUID ids with the ciphertext ``BYTEA``
carried verbatim, the per-row savepoint that isolates a durable ``UNIQUE (provider_id,
alias)`` collision to a single reported error while the rest still land, and the category
import's create/skip counts — behavior the fake pool cannot exhibit (the real constraint,
the real bytea, the real savepoint).

The SQL round-trip is pinned without a live store in ``test_store_backup.py``; only a real
database exercises the constraint + savepoint. It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1``
and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres (plus a live
``CONNECTOR_STORE_REDIS_URL`` for the cache invalidation). Without the opt-in it SKIPS
VISIBLY (never a silent skip)."""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, LiteralString

import pytest
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.settings import connector_store_settings
from tai42_skeleton.connectors.store.backup import (
    export_connector_connections,
    import_connector_categories,
    import_connector_connections,
)
from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _pg_exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@dataclass
class _Fixture:
    store: RedisPgConnectorTokenStore
    token: str
    cids: list[str] = field(default_factory=list)

    def cid(self) -> str:
        value = str(uuid.uuid4())
        self.cids.append(value)
        return value


@pytest.fixture
async def fix() -> AsyncIterator[_Fixture]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres connector backup round-trip is opt-in: set {_OPT_IN_ENV}=1, point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres and CONNECTOR_STORE_REDIS_URL at a live "
            "Redis to run it (needs the UNIQUE constraint + per-row savepoint + real bytea — no fake)"
        )
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    fixture = _Fixture(store=RedisPgConnectorTokenStore(), token=f"prov-it-{uuid.uuid4().hex[:12]}")
    await _wipe(fixture)
    yield fixture
    await _wipe(fixture)
    await shutdown_all_clients()


async def _wipe(fixture: _Fixture) -> None:
    await _pg_exec("DELETE FROM connector_connections WHERE provider_id = %s", (fixture.token,))
    await _pg_exec("DELETE FROM connector_category WHERE id LIKE %s", (f"cat-{fixture.token}%",))
    if fixture.cids:
        async with client_ctx(RedisClient, connector_store_settings().redis) as client:
            for cid in fixture.cids:
                await client.delete(fixture.store._rec_key(cid))


async def test_connection_records_round_trip_verbatim(fix: _Fixture) -> None:
    s, token = fix.store, fix.token
    first, second = fix.cid(), fix.cid()
    await s.put(first, b"cipher-1", create_only=True, provider_id=token, alias="a")
    await s.put(second, b"cipher-2", create_only=True, provider_id=token, alias="b")

    exported = [e for e in await export_connector_connections() if e["provider_id"] == token]
    assert len(exported) == 2
    # The ciphertext is carried base64 AS-IS — never decrypted, so the KEK boundary is never
    # crossed by the round-trip.
    assert {base64.b64decode(e["encrypted_blob_b64"]) for e in exported} == {b"cipher-1", b"cipher-2"}

    await _pg_exec("DELETE FROM connector_connections WHERE provider_id = %s", (token,))
    report = await import_connector_connections(exported)
    assert report["errors"] == []
    assert report["created"] == 2
    # The durable rows are back under their original UUID ids with the exact ciphertext.
    assert await s.get(first) == b"cipher-1"
    assert await s.get(second) == b"cipher-2"


async def test_alias_collision_isolated_to_one_reported_error(fix: _Fixture) -> None:
    s, token = fix.store, fix.token
    good, clash = fix.cid(), fix.cid()
    payload: list[dict[str, Any]] = [
        {
            "connection_id": good,
            "provider_id": token,
            "alias": "dup",
            "session_expires_at": None,
            "encrypted_blob_b64": base64.b64encode(b"good").decode("ascii"),
        },
        {
            # A DIFFERENT connection claiming the same (provider_id, alias): the real
            # ``UNIQUE (provider_id, alias)`` trips inside this row's savepoint, so it is
            # reported + skipped while the first row still commits.
            "connection_id": clash,
            "provider_id": token,
            "alias": "dup",
            "session_expires_at": None,
            "encrypted_blob_b64": base64.b64encode(b"clash").decode("ascii"),
        },
    ]
    report = await import_connector_connections(payload)
    assert report["created"] == 1
    assert report["skipped"] == 1
    reported = [err for err in report["errors"] if "dup" in err and clash in err]
    assert reported
    assert await s.get(good) == b"good"
    assert await s.get(clash) is None


async def test_category_import_creates_then_skips(fix: _Fixture) -> None:
    token = fix.token
    new_id = f"cat-{token}"
    payload = {
        "categories": [
            {
                "id": new_id,
                "display_name": "Custom",
                "sort_order": 42,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            }
        ]
    }
    created = await import_connector_categories(payload)
    assert created["created"] == 1
    assert await _category_display(new_id) == "Custom"
    # ``skip`` mode leaves an already-present id untouched — reported as skipped_existing.
    skipped = await import_connector_categories(payload, mode="skip")
    assert skipped["created"] == 0
    assert skipped["skipped_existing"] == 1


async def _category_display(category_id: str) -> str | None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT display_name FROM connector_category WHERE id = %s", (category_id,))
        row = await cur.fetchone()
    return None if row is None else row[0]
