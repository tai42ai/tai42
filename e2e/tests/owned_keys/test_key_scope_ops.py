"""Granular key-scope ops: add/remove ONE scope at a time on an api key
through ``POST /api/auth/api-keys/{user_id}/scopes``, instead of the whole-set replace
``PUT /api/auth/api-keys/{user_id}`` forces.

Driven over the seeded access-control stack as the root ``*`` admin, so the subset gate
(added scopes must be ⊆ the caller's) is bypassed — admin short-circuits it — and the per-op
membership rules — a present add is a loud 400, an absent remove a loud 404 — are what
the assertions isolate. The mutation response carries the new scope set (the read-back),
and the key's own ``/api/auth/me`` confirms the change took effect at the gate."""

from __future__ import annotations

from collections.abc import Callable

from _owned_support import mint_owner

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack


async def _register_scope(root: ApiClient, scope_id: str) -> None:
    """Register a scope so it may be assigned — a scope must exist before a key can hold
    it (the same precondition the whole-set ``PUT`` path enforces)."""
    await root.post("/api/auth/scopes", json={"scope_id": scope_id, "url": f"/e2e/{scope_id}"})


async def test_key_scopes_add_remove(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    root = auth_stack.api(port=auth_stack.port_a)

    first = uniq("scope")
    second = uniq("scope")
    third = uniq("scope")
    for scope in (first, second, third):
        await _register_scope(root, scope)

    user_id, raw = await mint_owner(root, uniq, scopes=[first, second])
    key = root.with_token(raw)

    # Add the third scope: the response carries the new set (stored order, addition
    # appended), and the key's own projection gains it at the gate.
    added = await root.post(f"/api/auth/api-keys/{user_id}/scopes", json={"add": [third]})
    assert added["user_id"] == user_id, added
    assert sorted(added["scopes"]) == sorted([first, second, third]), added

    async def key_sees(expected: list[str]) -> bool:
        return sorted((await key.get("/api/auth/me"))["scopes"]) == sorted(expected)

    await wait_for_async(
        lambda: key_sees([first, second, third]),
        deadline=5.0,
        message="the added scope never reached the key's projection",
    )

    # Remove one scope: the response drops exactly it, and the projection follows.
    removed = await root.post(f"/api/auth/api-keys/{user_id}/scopes", json={"remove": [second]})
    assert sorted(removed["scopes"]) == sorted([first, third]), removed
    await wait_for_async(
        lambda: key_sees([first, third]),
        deadline=5.0,
        message="the removed scope never left the key's projection",
    )

    # Adding an already-present scope is a loud 400 naming it (never a silent no-op).
    dup = await root.post(f"/api/auth/api-keys/{user_id}/scopes", json={"add": [first]}, expect=400)
    assert first in dup["error"], dup

    # Removing a scope the key does not hold is a loud 404 naming it.
    absent = uniq("scope")
    missing = await root.post(f"/api/auth/api-keys/{user_id}/scopes", json={"remove": [absent]}, expect=404)
    assert absent in missing["error"], missing
