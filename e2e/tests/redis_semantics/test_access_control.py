"""C6 — access control across replicas: a key provisioned on A authorizes on B
immediately (the selected identity provider reads its records fresh per call, from
whatever store it owns), and revocation propagates; concurrent disjoint-field policy
edits are both atomic against the Postgres policy store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import redis as redis_lib

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

# The scope seeded by the auth bootstrap (conftest ``_seed_bootstrap_key``): a
# catch-all route resolves every non-public path to it, and the root ``*``
# policy satisfies it, so a key minted with it can reach the admin routes.
_SCOPE = "e2e-all"


def _policy_version(stack: TaiStack) -> int:
    """Read the plain-Redis policy-version counter enforcement caches on."""
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        raw = cast("str | None", client.get("ac:policy_version"))
    finally:
        client.close()
    return int(raw) if raw is not None else 0


async def test_api_key_provisioned_on_a_authorizes_on_b_and_revocation_propagates(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    user = uniq("user")
    api_a = auth_stack.api(port=auth_stack.port_a)
    api_b = auth_stack.api(port=auth_stack.port_b)

    raw_key = (
        await api_a.post("/api/auth/api-keys", json={"user_id": user, "description": "e2e", "scopes": [_SCOPE]})
    )["api_key"]
    assert raw_key.startswith("sk-")

    # Authorizes on B immediately.
    caller_b = api_b.with_token(raw_key)
    await caller_b.get("/api/tools")

    # Revoke on B; A rejects with a tight deadline (policy version bump).
    await api_b.delete(f"/api/auth/api-keys/{user}")
    caller_a = api_a.with_token(raw_key)

    async def rejected_on_a() -> bool:
        response = await caller_a.request_raw("GET", "/api/tools")
        return response.status_code in (401, 403)

    await wait_for_async(rejected_on_a, deadline=5.0, message="revocation on B never propagated to A")


async def test_concurrent_policy_updates_are_atomic(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    user = uniq("user")
    api_a = auth_stack.api(port=auth_stack.port_a)
    api_b = auth_stack.api(port=auth_stack.port_b)

    # Register the scopes the scope-edit half toggles through (each scope must
    # exist before it can be assigned to a key). Done before the contention loop
    # so these version bumps do not perturb the per-round delta asserted below.
    scopes = [f"{uniq('scope')}" for _ in range(10)]
    for scope in scopes:
        await api_a.post("/api/auth/scopes", json={"scope_id": scope, "url": f"/e2e/{scope}"})

    await api_a.post("/api/auth/api-keys", json={"user_id": user, "description": "e2e", "scopes": [_SCOPE]})

    for round_i in range(10):
        scope = scopes[round_i]
        edit_b = {"description": f"desc-{round_i}", "policy_data": {"n": round_i}}
        before = _policy_version(auth_stack)
        await asyncio.gather(
            api_a.put(f"/api/auth/api-keys/{user}", json={"scopes": [_SCOPE, scope]}),
            api_b.put(f"/api/auth/api-keys/{user}", json=edit_b),
        )
        # Each edit bumps the version once; the two disjoint edits bump it twice.
        assert _policy_version(auth_stack) == before + 2, f"policy version did not bump twice in round {round_i}"

        payloads = await api_a.get("/api/auth/tokens-payload")
        row = next((p for p in payloads if p.get("user_id") == user), None)
        assert row is not None, f"policy row for {user} vanished in round {round_i}"
        # Neither the scope edit nor the description/policy_data edit was lost.
        assert scope in row.get("scopes", []), f"scope edit lost in round {round_i}: {row}"
        assert row.get("description") == f"desc-{round_i}", f"description edit lost in round {round_i}: {row}"
