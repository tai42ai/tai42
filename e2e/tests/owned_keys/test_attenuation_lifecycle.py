"""Owned-key attenuation over its lifecycle: subset-mint enforcement (a mint beyond
the parent's scopes is rejected naming the excess; an owned key can mint nothing), and
the two cross-worker attenuation-over-time cascades — shrinking the owner's scopes on
worker A collapses the owned key's reach on worker B to the intersection, and revoking
the owner kills the owned key on both workers."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._owned_support import SCOPE, mint_owned, mint_owner


async def test_mint_beyond_owner_scopes_is_rejected_naming_the_excess(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)

    excess_scope = uniq("scope")
    response = await owner.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": uniq("owned"), "description": "over-broad", "scopes": [SCOPE, excess_scope]},
    )
    assert response.status_code == 400, response.text
    # The refusal must name the offending scope, not deny opaquely.
    assert excess_scope in response.text


async def test_owned_key_may_mint_nothing(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = auth_stack.api(port=auth_stack.port_a)
    _owner_id, owner_raw = await mint_owner(root, uniq)
    owner = root.with_token(owner_raw)
    _owned_id, owned_raw = await mint_owned(owner, uniq)
    owned = root.with_token(owned_raw)

    response = await owned.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": uniq("grand"), "description": "second-level", "scopes": [SCOPE]},
    )
    assert response.status_code == 403, response.text


async def test_owner_scope_shrink_attenuates_owned_key_cross_worker(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root_a = auth_stack.api(port=auth_stack.port_a)
    owner_id, owner_raw = await mint_owner(root_a, uniq)
    owner = root_a.with_token(owner_raw)
    _owned_id, owned_raw = await mint_owned(owner, uniq)
    owned_b = auth_stack.api(port=auth_stack.port_b).with_token(owned_raw)

    # Sentinel: the owned key reaches a protected route on B before the shrink.
    before = await owned_b.request_raw("GET", "/api/tools")
    assert before.status_code == 200, before.text

    # Shrink the owner on A to a fresh, unrelated scope (registered first, as a scope
    # must exist before it can be assigned). The owned key keeps ``e2e-all``, but its
    # OWNER no longer holds it, so the request-time intersection empties.
    other_scope = uniq("scope")
    await root_a.post("/api/auth/scopes", json={"scope_id": other_scope, "url": f"/e2e/{other_scope}"})
    await root_a.put(f"/api/auth/api-keys/{owner_id}", json={"scopes": [other_scope]})

    async def denied_on_b() -> bool:
        response = await owned_b.request_raw("GET", "/api/tools")
        return response.status_code == 403

    await wait_for_async(denied_on_b, deadline=5.0, message="owner scope shrink never attenuated the owned key on B")


async def test_owner_revoke_kills_owned_key_on_both_workers(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root_a = auth_stack.api(port=auth_stack.port_a)
    owner_id, owner_raw = await mint_owner(root_a, uniq)
    owner = root_a.with_token(owner_raw)
    _owned_id, owned_raw = await mint_owned(owner, uniq)
    owned_a = auth_stack.api(port=auth_stack.port_a).with_token(owned_raw)
    owned_b = auth_stack.api(port=auth_stack.port_b).with_token(owned_raw)

    # Sentinel: the owned key is live on both workers before the owner dies.
    for owned in (owned_a, owned_b):
        alive = await owned.request_raw("GET", "/api/tools")
        assert alive.status_code == 200, alive.text

    await root_a.delete(f"/api/auth/api-keys/{owner_id}")

    # Owner-death attenuation: a missing owner policy denies the owned key (403) on
    # every worker, within the policy-version propagation deadline.
    for label, owned in (("A", owned_a), ("B", owned_b)):

        async def dead(owned=owned) -> bool:
            response = await owned.request_raw("GET", "/api/tools")
            return response.status_code == 403

        await wait_for_async(dead, deadline=5.0, message=f"owner revoke never killed the owned key on {label}")
