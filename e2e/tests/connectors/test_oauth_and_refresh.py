"""C6 + C7 (P1) — OAuth connect started on replica A, completed on replica B,
against the stub IdP; the stored token is encrypted at rest; a concurrent refresh
takes the Redis lock exactly once.

The connector-provider wiring and the ``/oauth/complete`` signed-state envelope
are the highest-uncertainty seams in the suite (see the repo README notes)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg

from tai42_e2e.netfixtures import OAuthIdp
from tai42_e2e.stack import TaiStack


async def _authorize(authorize_url: str) -> tuple[str, str]:
    """Follow the authorize URL to the stub IdP and recover (code, state) from
    its redirect back to the connector callback."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        response = await client.get(authorize_url)
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["code"][0], query["state"][0]


def _pg(stack: TaiStack) -> psycopg.Connection:
    return psycopg.connect(
        host=stack.resources.pg_host,
        port=stack.resources.pg_port,
        user=stack.resources.pg_user,
        password=stack.resources.pg_password,
        dbname=stack.resources.pg_db,
    )


async def test_oauth_connect_started_on_a_completed_on_b(
    connectors_stack: TaiStack, oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    alias = uniq("conn")
    api_a = connectors_stack.api(port=connectors_stack.port_a)
    api_b = connectors_stack.api(port=connectors_stack.port_b)

    start = await api_a.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_idp", "alias": alias, "enabled_sub_services": ["default"]},
    )
    code, state = await _authorize(start["authorize_url"])

    completed = await api_b.post("/api/connectors/oauth/complete", json={"code": code, "state": state})
    assert completed["kind"] == "success", f"oauth completion failed: {completed}"

    # The connection created on the B-completed flow is visible from A (durable
    # store, not a per-worker cache).
    listed = await api_a.get("/api/connectors/connections")
    aliases = [c.get("alias") for c in listed["items"]]
    assert alias in aliases, f"connection {alias!r} not visible on A: {listed}"

    # The token is ciphertext at rest — the IdP-issued token must not appear
    # verbatim in the stored blob.
    with _pg(connectors_stack) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_blob FROM connector_connections WHERE provider_id = %s AND alias = %s", ("e2e_idp", alias)
        )
        row = cur.fetchone()
    assert row is not None, "no connection row was written"
    blob = bytes(row[0])
    assert oauth_idp.issued_access_tokens, "the stub IdP issued no access token during the connect flow"
    for token in oauth_idp.issued_access_tokens:
        assert token.encode() not in blob, "the IdP access token is stored in plaintext (not encrypted at rest)"


async def test_concurrent_refresh_takes_lock_once(
    connectors_stack: TaiStack, oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    # The next-issued access token is already expired, forcing a refresh on use.
    oauth_idp.set_expires_in(-1)
    alias = uniq("conn")
    api_a = connectors_stack.api(port=connectors_stack.port_a)
    start = await api_a.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_idp", "alias": alias, "enabled_sub_services": ["default"]},
    )
    code, state = await _authorize(start["authorize_url"])
    await connectors_stack.api(port=connectors_stack.port_b).post(
        "/api/connectors/oauth/complete", json={"code": code, "state": state}
    )
    # A fresh lifetime so the forced refresh yields a non-expired token.
    oauth_idp.set_expires_in(3600)

    with _pg(connectors_stack) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT connection_id FROM connector_connections WHERE provider_id = %s AND alias = %s", ("e2e_idp", alias)
        )
        row = cur.fetchone()
    assert row is not None, "connection row was not written"
    connection_id = str(row[0])

    # Resolve the (expired) token concurrently on both replicas: the cross-process
    # connection lock serializes the read-modify-write so exactly ONE upstream
    # refresh happens, and both callers observe a resolved token.
    before = oauth_idp.refresh_count

    async def resolve(port: int) -> dict:
        async with connectors_stack.mcp(port=port) as mcp:
            result = await mcp.call_tool("e2e_resolve_connector", {"connection_id": connection_id})
        return result.data

    results = await asyncio.gather(
        resolve(connectors_stack.port_a),
        resolve(connectors_stack.port_b),
    )
    assert all(r.get("resolved") for r in results), f"a connector resolve returned no token: {results}"
    assert oauth_idp.refresh_count == before + 1, (
        f"expected exactly one upstream refresh under the lock, saw {oauth_idp.refresh_count - before}"
    )
