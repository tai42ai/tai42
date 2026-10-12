"""Disconnect of a connection whose stored client is dead or gone, with the REAL client store on Postgres.

The token store and the provider registry are the service harness's; the client the grant names is read
from the real ``connector_oauth_clients`` table. Opt-in like its ``*_integration.py`` siblings: set
``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres; without them the
test SKIPS VISIBLY.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from tai42_contract.connectors.models import AuthHealthState, ConnectionGrant, ConnectionRecord
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.oauth import discovery
from tai42_skeleton.connectors.oauth.registration import RegisteredClient
from tai42_skeleton.connectors.service.connection_service import disconnect
from tai42_skeleton.connectors.store.client_store import ConnectorClientStore
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry

from ..conftest import CID, make_discovered_descriptor
from ..oauth_fakes import FakeOAuthServer, Reply, serve

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"
CALLBACK = "https://app.example.com/oauth-bridge.html"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOAuthServer]:
    monkeypatch.setenv("TAI_URL_GUARD_ALLOW_CIDRS", '["127.0.0.0/8"]')
    reset_all_settings()
    discovery.reset_discovery_cache()
    yield from serve()
    discovery.reset_discovery_cache()


@pytest.fixture
async def provider_id() -> AsyncIterator[str]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"the real-Postgres client store disconnect test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres"
        )
    await apply_migrations([skeleton_entry()])
    token = f"discit{uuid.uuid4().hex[:12]}"
    yield token
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute("DELETE FROM connector_oauth_clients WHERE provider_id = %s", (token,))
    await shutdown_all_clients()


@pytest.mark.parametrize("state", ["dead", "deleted"])
async def test_a_connection_whose_client_is_dead_or_deleted_is_removed(
    harness, fake: FakeOAuthServer, provider_id: str, state: str
):
    _, records, store, _, _, providers = harness
    issuer = f"{fake.base}/a"
    mcp = f"{fake.base}/mcp"
    fake.protected_mcp("/mcp", pointer=f"{fake.base}/prm")
    fake.json("/prm", {"resource": mcp, "authorization_servers": [issuer]})
    fake.authorization_server("/a", revocation_endpoint=f"{issuer}/revoke")
    fake.route("POST", "/a/revoke", Reply(status=200))
    providers[provider_id] = make_discovered_descriptor(provider_id=provider_id, mcp_url=mcp)

    clients = ConnectorClientStore()
    row = await clients.add_dynamic(
        provider_id,
        issuer,
        RegisteredClient(
            client_id="dyn-1",
            client_secret="dyn-secret",
            auth_method="client_secret_post",
            secret_expires_at=None,
            redirect_uri=CALLBACK,
        ),
    )
    if state == "dead":
        assert await clients.mark_dead(row.client_row_id)
    else:
        assert await clients.delete_for_provider(provider_id) == 1

    records[CID] = ConnectionRecord(
        connection_id=CID,
        provider_id=provider_id,
        kind="oauth",
        alias="team",
        account_identity="a@example.com",
        enabled_sub_services=["files"],
        granted_scopes=["files.read"],
        access_token=SecretStr("old-at"),
        refresh_token=SecretStr("old-rt"),
        auth_health_state=AuthHealthState.HEALTHY,
        grant=ConnectionGrant(issuer=issuer, client_id="dyn-1"),
        created_at=datetime.now(UTC),
    )
    result = await disconnect(connection_id=CID)

    assert result.upstream_revoke_outcome == "failed"
    assert fake.requests_to("/a/revoke") == []
    assert CID in store.deleted
