"""The REAL connection lock on a REAL Redis gives its key back whatever cancels its holder.

A holder cancelled by ``asyncio.wait_for`` and one cancelled by an anyio cancel scope (which delivers
its cancellation again at every wake-up) each leave the key absent, so the next acquire succeeds at
once; the same for a full refresh driven through the real lock. Opt-in like its ``*_integration.py``
siblings: set ``TAI42_SKELETON_REAL_PG=1`` and ``CONNECTOR_STORE_REDIS_URL``; without them the tests
SKIP VISIBLY.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.connectors.oauth import crypto, discovery
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.runtime.locks import _acquire, _lock_key, _release, connection_lock
from tai42_skeleton.connectors.settings import connector_store_settings

from .conftest import make_discovered_descriptor
from .oauth_fakes import FakeOAuthServer, Reply, serve

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


@pytest.fixture
async def real_redis() -> AsyncIterator[list[str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True") or not os.environ.get("CONNECTOR_STORE_REDIS_URL"):
        pytest.skip(
            f"real-Redis connection-lock test is opt-in: set {_OPT_IN_ENV}=1 and point CONNECTOR_STORE_REDIS_URL "
            "at a live Redis (the lock's release under cancellation needs a real round trip — no fake)"
        )
    reset_all_settings()
    touched: list[str] = []
    yield touched
    async with client_ctx(RedisClient, connector_store_settings().redis) as client:
        for cid in touched:
            await client.delete(_lock_key(cid))
    await shutdown_all_clients()


async def _key(cid: str) -> bytes | None:
    async with client_ctx(RedisClient, connector_store_settings().redis) as client:
        return await client.get(_lock_key(cid))


def _cid(touched: list[str]) -> str:
    cid = str(uuid.uuid4())
    touched.append(cid)
    return cid


async def _hold(cid: str, held: asyncio.Event) -> None:
    async with connection_lock(cid):
        held.set()
        await asyncio.sleep(30)


async def test_a_holder_cancelled_by_wait_for_gives_the_key_back(real_redis: list[str]) -> None:
    cid = _cid(real_redis)
    held = asyncio.Event()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_hold(cid, held), 0.3)
    assert held.is_set()
    assert await _key(cid) is None
    started = time.monotonic()
    token = await _acquire(cid)
    assert token is not None
    assert time.monotonic() - started < 0.5
    await _release(cid, token)


async def test_a_holder_cancelled_by_an_anyio_scope_gives_the_key_back(real_redis: list[str]) -> None:
    cid = _cid(real_redis)
    held = asyncio.Event()
    with anyio.move_on_after(0.3) as scope:
        await _hold(cid, held)
    assert scope.cancelled_caught
    assert held.is_set()
    assert await _key(cid) is None
    started = time.monotonic()
    token = await _acquire(cid)
    assert token is not None
    assert time.monotonic() - started < 0.5
    await _release(cid, token)


# -- the full refresh through the real lock ---------------------------------------------------


@pytest.fixture
def fake() -> Iterator[FakeOAuthServer]:
    yield from serve()


class _Store:
    def __init__(self, record: ConnectionRecord) -> None:
        self.blob = crypto.encrypt(record.to_storage_json().encode(), connection_id=record.connection_id)
        self.cid = record.connection_id

    def record(self) -> ConnectionRecord:
        return ConnectionRecord.model_validate_json(crypto.decrypt(self.blob, connection_id=self.cid))

    async def put(self, connection_id, blob, *, create_only=False, expected_blob=None, session_expires_at=None):
        if expected_blob is not None and expected_blob != self.blob:
            return False
        self.blob = blob
        return True


@pytest.mark.parametrize("cancel", ["wait_for", "anyio"])
async def test_a_cancelled_refresh_through_the_real_lock_finishes_and_gives_the_key_back(
    real_redis: list[str], fake: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch, cancel: str
) -> None:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.1/32"]')
    monkeypatch.setenv("REMOTE_CLIENT_ID", "remote-env-id")
    monkeypatch.setenv("REMOTE_CLIENT_SECRET", "remote-env-secret")
    reset_all_settings()
    discovery.reset_discovery_cache()
    refresh_owner.reset()
    cid = _cid(real_redis)
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [f"{fake.base}/a"]})
    fake.authorization_server("/a")
    fake.route(
        "POST",
        "/a/token",
        Reply(delay=0.6, body={"access_token": "at-new", "refresh_token": "rt-rotated", "expires_in": 3600}),
    )
    now = datetime.now(UTC)
    store = _Store(
        ConnectionRecord(
            connection_id=cid,
            provider_id="remote",
            kind="oauth",
            alias="team",
            account_identity="a@example.com",
            enabled_sub_services=["files"],
            access_token=SecretStr("at-old"),
            refresh_token=SecretStr("rt-old"),
            access_token_expires_at=now - timedelta(seconds=5),
            auth_health_state=AuthHealthState.HEALTHY,
            grant=ConnectionGrant(issuer=f"{fake.base}/a"),
            created_at=now,
        )
    )

    async def _load(connection_id):
        return store.record(), store.blob

    monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
    monkeypatch.setattr(resolver_mod, "token_store", lambda: store)
    monkeypatch.setattr(
        resolver_mod, "get_provider", lambda pid: make_discovered_descriptor(mcp_url=mcp, issuer=f"{fake.base}/a")
    )
    await discovery.discover(mcp, bound_issuer=f"{fake.base}/a")
    if cancel == "wait_for":
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(resolver_mod.resolve_managed_auth(cid, "remote", "files"), 0.2)
    else:
        with anyio.move_on_after(0.2) as scope:
            await resolver_mod.resolve_managed_auth(cid, "remote", "files")
        assert scope.cancelled_caught
    stored = store.record()
    assert stored.refresh_token is not None
    assert stored.refresh_token.get_secret_value() == "rt-rotated"
    assert await _key(cid) is None
    token = await _acquire(cid)
    assert token is not None
    await _release(cid, token)
    discovery.reset_discovery_cache()
