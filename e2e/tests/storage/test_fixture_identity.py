"""The fixture Postgres-backed identity provider (identity provider #2)
proves the identity-axis switch actually switched: identity records land in
Postgres, NOT in the redis provider's ``ac:key:*`` hashes, and core's ``/ready``
health-checks the provider's Postgres store through the generic readiness seam.

Fixture-identity only: on the redis-identity leg these skip loudly (the axis is
not the one under test). They ride the fixture leg's
``tests/redis_semantics tests/storage tests/harness`` selection."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import psycopg
import pytest
import redis as redis_lib

from tai42_e2e.stack import TaiStack

# The scope the auth bootstrap seeds (a catch-all route resolves every non-public
# path to it, and the root ``*`` policy satisfies it), so a key minted with it
# reaches the admin routes.
_SCOPE = "e2e-all"

pytestmark = pytest.mark.usefixtures("auth_stack")


def _skip_unless_fixture(stack: TaiStack) -> None:
    if stack.infra.variants.identity.name != "fixture":
        pytest.skip("fixture-identity spec; runs only on TAI_E2E_IDENTITY=fixture")


def _redis_identity_keys(stack: TaiStack) -> list[str]:
    """Scan the stack's Redis logical DB for the redis identity provider's
    ``ac:key:*`` record hashes — empty on the fixture leg proves identity routed
    to Postgres, not Redis."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        return sorted(cast("list[str]", list(client.scan_iter(match="ac:key:*"))))
    finally:
        client.close()


def _pg_identity_user_ids(stack: TaiStack) -> list[str]:
    """The user ids in the fixture provider's Postgres table."""
    res = stack.resources
    with (
        psycopg.connect(
            host=res.pg_host, port=res.pg_port, user=res.pg_user, password=res.pg_password, dbname=res.pg_db
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT user_id FROM fixture_identity_keys")
        return sorted(row[0] for row in cur.fetchall())


async def test_identity_records_live_in_pg_not_redis(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    _skip_unless_fixture(auth_stack)
    user = uniq("user")
    api = auth_stack.api()

    raw_key = (await api.post("/api/auth/api-keys", json={"user_id": user, "description": "e2e", "scopes": [_SCOPE]}))[
        "api_key"
    ]
    assert raw_key.startswith("sk-")

    # The minted key authorizes through the public surface (the provider validated
    # it against its Postgres store).
    await api.with_token(raw_key).get("/api/tools")

    # The identity record is in Postgres...
    assert user in _pg_identity_user_ids(auth_stack), "minted identity did not land in the fixture Postgres table"
    # ...and NO identity record hash appears in Redis (the axis really switched).
    assert _redis_identity_keys(auth_stack) == [], "fixture-identity leg wrote ac:key:* records into Redis"


async def test_absent_key_is_rejected(auth_stack: TaiStack) -> None:
    _skip_unless_fixture(auth_stack)
    # A token the provider has no record for is rejected at the public surface — the
    # provider did not silently fall back to the redis provider (which has no record
    # either, but the point is the fixture provider owns the deny).
    response = await auth_stack.api().with_token("sk-absent-not-a-real-key").request_raw("GET", "/api/tools")
    assert response.status_code in (401, 403), f"absent key was not rejected: {response.status_code} {response.text}"
    assert _redis_identity_keys(auth_stack) == [], "fixture-identity leg wrote ac:key:* records into Redis"


async def test_ready_reports_fixture_identity_readiness_target(auth_stack: TaiStack) -> None:
    _skip_unless_fixture(auth_stack)
    # Core's /ready enumerates the ACTIVE identity provider's readiness targets
    # generically; the fixture provider declares its Postgres store, which pings
    # clean, so the "access_control" check is present and ok.
    response = await auth_stack.api().request_raw("GET", "/ready")
    assert response.status_code == 200, f"/ready not ready: {response.status_code} {response.text}"
    body = response.json()
    assert body["status"] == "ready", body
    assert body["checks"].get("access_control") == "ok", f"fixture identity readiness target not ok: {body}"
