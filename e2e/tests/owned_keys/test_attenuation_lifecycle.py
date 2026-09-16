"""Owned-key attenuation across the mint and the owner lifecycle. At mint: a mint from
below beyond the owner's scopes is rejected naming the excess, an owned key can mint
nothing, and a non-admin principal may name only itself as owner. Over the owner's
lifecycle, cross-worker: shrinking the owner principal's scopes on worker A collapses
the owned key's reach on worker B to the intersection (403), disabling the owner denies
its key on both workers (403), and deleting the owner revokes its key on both (401 — the
identity record goes with the principal)."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

from ._owned_support import SCOPE, create_service_owner, mint_key_for, mint_owned, provision_owner


async def test_mint_beyond_owner_scopes_is_rejected_naming_the_excess(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    _owner_id, session = await provision_owner(owned_keys_stack, root, uniq, scopes=[SCOPE])
    owner = root.with_token(session)

    excess_scope = uniq("scope")
    response = await owner.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": uniq("owned"), "description": "over-broad", "scopes": [SCOPE, excess_scope]},
    )
    assert response.status_code == 400, response.text
    # The refusal must name the offending scope, not deny opaquely.
    assert excess_scope in response.text


async def test_owned_key_may_mint_nothing(owned_keys_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    _owner_id, session = await provision_owner(owned_keys_stack, root, uniq)
    owner = root.with_token(session)
    _owned_id, owned_raw = await mint_owned(owner, uniq)
    owned = root.with_token(owned_raw)

    response = await owned.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": uniq("grand"), "description": "second-level", "scopes": [SCOPE]},
    )
    assert response.status_code == 403, response.text
    # Every non-admin key carries an owner claim, so it is barred from minting at all.
    assert "an owned API key may not mint API keys" in response.text


async def test_principal_mints_only_self_owned_keys(owned_keys_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    _owner_id, session = await provision_owner(owned_keys_stack, root, uniq, scopes=[SCOPE])
    owner = root.with_token(session)
    other_owner_id = await create_service_owner(root, uniq)

    # A non-admin top-level principal may mint only self-owned keys, so naming another
    # principal as the owner is a loud refusal.
    response = await owner.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={
            "user_id": uniq("owned"),
            "description": "for another",
            "scopes": [SCOPE],
            "owner_user_id": other_owner_id,
        },
    )
    assert response.status_code == 403, response.text
    assert "may only create keys owned by itself" in response.text


async def test_owner_scope_shrink_attenuates_owned_key_cross_worker(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root_a = owned_keys_stack.api(port=owned_keys_stack.port_a)
    svc_id = await create_service_owner(root_a, uniq)
    _owned_id, owned_raw = await mint_key_for(root_a, uniq, svc_id, scopes=[SCOPE])
    owned_b = owned_keys_stack.api(port=owned_keys_stack.port_b).with_token(owned_raw)

    # Sentinel: the owned key reaches a protected route on B before the shrink.
    before = await owned_b.request_raw("GET", "/api/tools")
    assert before.status_code == 200, before.text

    # Shrink the owner principal on A to a fresh, unrelated scope (registered first, as a
    # scope must exist before it can be assigned). The owned key keeps ``e2e-all``, but its
    # OWNER no longer holds it, so the request-time intersection empties.
    other_scope = uniq("scope")
    await root_a.post("/api/auth/scopes", json={"scope_id": other_scope, "url": f"/e2e/{other_scope}"})
    await root_a.put(f"/api/auth/api-keys/{svc_id}", json={"scopes": [other_scope]})

    async def denied_on_b() -> bool:
        response = await owned_b.request_raw("GET", "/api/tools")
        return response.status_code == 403

    await wait_for_async(denied_on_b, deadline=5.0, message="owner scope shrink never attenuated the owned key on B")


async def test_owner_disable_denies_owned_key_cross_worker(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root_a = owned_keys_stack.api(port=owned_keys_stack.port_a)
    svc_id = await create_service_owner(root_a, uniq)
    _owned_id, owned_raw = await mint_key_for(root_a, uniq, svc_id, scopes=[SCOPE])
    owned_a = owned_keys_stack.api(port=owned_keys_stack.port_a).with_token(owned_raw)
    owned_b = owned_keys_stack.api(port=owned_keys_stack.port_b).with_token(owned_raw)

    # Sentinel: the owned key is live on both workers before the owner is disabled.
    for owned in (owned_a, owned_b):
        alive = await owned.request_raw("GET", "/api/tools")
        assert alive.status_code == 200, alive.text

    await root_a.put(f"/api/auth/principals/{svc_id}", json={"disabled": True})

    # Owner-disabled deny: the owner's policy still exists but is disabled, so the owned
    # key is refused (403) on every worker within the policy-version propagation deadline.
    for label, owned in (("A", owned_a), ("B", owned_b)):

        async def disabled(owned=owned) -> bool:
            response = await owned.request_raw("GET", "/api/tools")
            return response.status_code == 403

        await wait_for_async(disabled, deadline=5.0, message=f"owner disable never denied the owned key on {label}")


async def test_owner_delete_revokes_owned_key_on_both_workers(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    root_a = owned_keys_stack.api(port=owned_keys_stack.port_a)
    svc_id = await create_service_owner(root_a, uniq)
    _owned_id, owned_raw = await mint_key_for(root_a, uniq, svc_id, scopes=[SCOPE])
    owned_a = owned_keys_stack.api(port=owned_keys_stack.port_a).with_token(owned_raw)
    owned_b = owned_keys_stack.api(port=owned_keys_stack.port_b).with_token(owned_raw)

    # Sentinel: the owned key is live on both workers before the owner dies.
    for owned in (owned_a, owned_b):
        alive = await owned.request_raw("GET", "/api/tools")
        assert alive.status_code == 200, alive.text

    await root_a.delete(f"/api/auth/principals/{svc_id}")

    # Deleting the owner principal revokes every key it owns, so the owned key's identity
    # record is gone and it answers 401 on every worker within the propagation deadline.
    for label, owned in (("A", owned_a), ("B", owned_b)):

        async def dead(owned=owned) -> bool:
            response = await owned.request_raw("GET", "/api/tools")
            return response.status_code == 401

        await wait_for_async(dead, deadline=5.0, message=f"owner delete never revoked the owned key on {label}")
