"""Use keeps a connection alive on the REAL token store: a used record about to lapse is still listed after
one more use, and two processes serving one record write it once (the Redis marker holds across them).

Opt-in like its ``*_integration.py`` siblings: set ``TAI42_SKELETON_REAL_PG=1``, the
``TAI_DATABASE_DEFAULT_PG_*`` env and ``CONNECTOR_STORE_REDIS_URL``; without them the tests SKIP VISIBLY.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionRecord
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.settings import connector_store_settings
from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry

from .conftest import make_oauth_descriptor

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


@pytest.fixture
async def real_store() -> AsyncIterator[tuple[RedisPgConnectorTokenStore, str, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-store keep-alive test is opt-in: set {_OPT_IN_ENV}=1, point the TAI_DATABASE_DEFAULT_PG_* env at "
            "a live Postgres and CONNECTOR_STORE_REDIS_URL at a live Redis (the session-expiry filter and the "
            "cross-process marker need the real store — no fake)"
        )
    reset_all_settings()
    refresh_owner.reset()
    resolver_mod.reset_keep_alive_stamps()
    await apply_migrations([skeleton_entry()])
    store = RedisPgConnectorTokenStore()
    provider = f"kalive{uuid.uuid4().hex[:10]}"
    cid = str(uuid.uuid4())
    yield store, provider, cid
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute("DELETE FROM connector_connections WHERE provider_id = %s", (provider,))
    async with client_ctx(RedisClient, connector_store_settings().redis) as client:
        await client.delete(store._rec_key(cid))
        await client.delete(f"{connector_store_settings().key_prefix}keep_alive:{cid}")
    resolver_mod.reset_keep_alive_stamps()
    await shutdown_all_clients()


async def _seed(store: RedisPgConnectorTokenStore, provider: str, cid: str, *, session_expires_at: datetime) -> bytes:
    now = datetime.now(UTC)
    record = ConnectionRecord(
        connection_id=cid,
        provider_id=provider,
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=["mail"],
        access_token=SecretStr("long-lived-at"),
        access_token_expires_at=None,
        auth_health_state=AuthHealthState.HEALTHY,
        created_at=now,
    )
    blob = crypto.encrypt(record.to_storage_json().encode(), connection_id=cid)
    await store.put(
        cid, blob, create_only=True, provider_id=provider, alias="team", session_expires_at=session_expires_at
    )
    return blob


async def _version(cid: str) -> int:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        cursor = await conn.execute("SELECT cache_version FROM connector_connections WHERE connection_id = %s", (cid,))
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def test_a_used_record_about_to_lapse_is_still_listed_after_one_more_use(real_store, monkeypatch) -> None:
    store, provider, cid = real_store
    descriptor = make_oauth_descriptor(provider_id=provider)
    monkeypatch.setattr(resolver_mod, "get_provider", lambda pid: descriptor)
    blob = await _seed(store, provider, cid, session_expires_at=datetime.now(UTC) + timedelta(seconds=2))
    auth = await resolver_mod.resolve_managed_auth(cid, provider, "mail")
    assert auth is not None
    await asyncio.sleep(2.5)
    assert cid in await store.list()
    # The stored ciphertext is byte-identical: only the session expiry moved.
    assert await store.get(cid) == blob


async def test_two_processes_serving_one_record_write_it_once(real_store, monkeypatch) -> None:
    store, provider, cid = real_store
    descriptor = make_oauth_descriptor(provider_id=provider)
    monkeypatch.setattr(resolver_mod, "get_provider", lambda pid: descriptor)
    await _seed(store, provider, cid, session_expires_at=datetime.now(UTC) + timedelta(days=1))
    seeded = await _version(cid)
    await resolver_mod.resolve_managed_auth(cid, provider, "mail")
    resolver_mod.reset_keep_alive_stamps()  # a second process: its own map, the same Redis
    await resolver_mod.resolve_managed_auth(cid, provider, "mail")
    assert await _version(cid) == seeded + 1
