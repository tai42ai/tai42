"""Granular role-grant ops (M36 F): set/remove a SINGLE tag grant on a role through
``POST /api/auth/roles/{name}/grants``, instead of the whole-map replace ``PUT
/api/auth/roles/{name}`` forces.

Driven over the seeded access-control stack as the root ``*`` admin (role management is
an admin-only ``fenced`` surface). ``--set`` is the mission's one sanctioned overwrite (a
map upsert), while ``remove`` of a tag the map does not carry is a loud 404 naming it.
The mutation response is the updated role body; the roles listing confirms it persisted.
"""

from __future__ import annotations

from collections.abc import Callable

from _rbac_support import create_role

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack


async def _role_grants(admin: ApiClient, name: str) -> dict[str, str]:
    """The persisted grant map of ``name``, read from the roles listing (no single-role
    GET exists) — the fleet-wide persistence cross-check for the mutation response."""
    roles = await admin.get("/api/auth/roles")
    entry = next(role for role in roles if role["name"] == name)
    return entry["grants"]


async def test_role_grants_set_remove(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    admin = accounts_stack.api(port=accounts_stack.port_a)

    name = uniq("role")
    await create_role(admin, name=name, base_tier="editor", grants={"hooks": "read"})

    # --set a second tag: the upsert adds it, leaving the existing grant intact.
    added = await admin.post(f"/api/auth/roles/{name}/grants", json={"set": {"presets": "write"}})
    assert added["grants"] == {"hooks": "read", "presets": "write"}, added
    assert await _role_grants(admin, name) == {"hooks": "read", "presets": "write"}

    # Remove the first tag: only it drops.
    removed = await admin.post(f"/api/auth/roles/{name}/grants", json={"remove": ["hooks"]})
    assert removed["grants"] == {"presets": "write"}, removed
    assert await _role_grants(admin, name) == {"presets": "write"}

    # Removing a tag the map no longer carries is a loud 404 naming it.
    missing = await admin.post(f"/api/auth/roles/{name}/grants", json={"remove": ["hooks"]}, expect=404)
    assert "hooks" in missing["error"], missing
