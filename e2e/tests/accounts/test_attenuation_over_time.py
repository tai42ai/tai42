"""C-accounts — an owned key is attenuated by its owner's CURRENT policy.

An owned key's authority is capped by the owner's live role at request time, not
frozen at mint: demoting the owner (editor -> viewer, a jq-condition swap) revokes
a POST the key could make moments earlier, and disabling the owner kills the key
outright. The change propagates cross-worker through the policy-version bump."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

_SCOPE = "e2e-all"


async def test_owned_key_follows_owner_role_over_time(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key (unconditional "*" admin)

    # An editor owner with a live policy (create-user applies the role), and a
    # machine key owned by that editor.
    owner = await admin.post("/api/auth/users", json={"email": f"{uniq('owner')}@e2e.test", "role": "editor"})
    owner_id = owner["user_id"]
    owned_id = uniq("owned")
    created_key = await admin.post(
        "/api/auth/api-keys",
        json={
            "user_id": owned_id,
            "description": "editor-owned key",
            "scopes": [_SCOPE],
            "owner_user_id": owner_id,
        },
    )
    key_b = stack.api(port=stack.port_b).with_token(created_key["api_key"])

    # Editor allows a POST outside /api/auth: the owned key creates a hook, binding its
    # own identity as the execution_key.
    created = await key_b.request_raw(
        "POST",
        "/api/hooks",
        json={
            "name": uniq("hook"),
            "topic": uniq("t").replace("_", "-"),
            "tool": "e2e_echo",
            "tool_kwargs": {},
            "execution_key": owned_id,
        },
    )
    assert created.status_code == 200, f"an editor-owned key must POST /api/hooks: {created.status_code} {created.text}"

    # Demote the owner to viewer — a jq-CONDITION swap on the owner's policy, not a
    # scope change. The same POST is now denied through the key (owner attenuation),
    # once the policy-version bump propagates to replica B.
    await admin.put(f"/api/auth/users/{owner_id}", json={"role": "viewer"})

    async def hook_post_denied() -> bool:
        response = await key_b.request_raw(
            "POST",
            "/api/hooks",
            json={
                "name": uniq("hook"),
                "topic": uniq("t").replace("_", "-"),
                "tool": "e2e_echo",
                "tool_kwargs": {},
                "execution_key": owned_id,
            },
        )
        return response.status_code == 403

    await wait_for_async(
        hook_post_denied,
        deadline=10.0,
        message="the owned key was never denied the POST after the owner became a viewer",
    )

    # A viewer still permits read-only GETs through the key (sentinel: alive before dead).
    alive = await key_b.request_raw("GET", "/api/system/kinds")
    assert alive.status_code == 200, f"a viewer-owned key must still GET: {alive.status_code} {alive.text}"

    # Disable the owner — the key is now dead entirely, even for a GET the viewer allowed.
    await admin.put(f"/api/auth/users/{owner_id}", json={"disabled": True})

    async def get_denied() -> bool:
        response = await key_b.request_raw("GET", "/api/system/kinds")
        return response.status_code in (401, 403)

    await wait_for_async(
        get_denied,
        deadline=10.0,
        message="the owned key was never killed after the owner was disabled",
    )
