"""Shared arrange helpers for the editable-RBAC (M20 F1) level journeys.

The skeleton's editable RBAC is a per-tag ACCESS-LEVEL map on top of a base tier: a
role is ``{feature-tag: none|read|write}`` (the editable ``grants``) intersected with a
seed-fixed base-tier jq ceiling (``editor``/``viewer``) AND the route action-class fence
(``fenced``/``secret`` are admin-only, never opened by a level). These helpers create a
role over the real admin API, assign it to a fresh accounts user (through the invite
accept → session leg), and mint an owner-attenuated key — the three ways a principal
comes to hold a role's grant map, so a spec asserts enforcement against the CURRENT map.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The mintable scope every seeded e2e route table maps its routes to (the catch-all the
# ``seed_bootstrap_key`` / accounts seed installs); an owned key minted with it passes
# the scope tier so the per-tag LEVEL gate — not a scope miss — is what a denial proves.
OWNED_KEY_SCOPE = "e2e-all"


# A valid ``POST /api/hooks`` body (a topic uses dashes, never underscores) — the one
# write whose success shape the suite already proves (``tai42_e2e_fixtures`` registers
# ``e2e_echo``), used to assert a WRITE-level grant actually reaches its writes.
# ``execution_key`` is the key user_id the background hook fires as; the caller must be
# able to bind it (its own identity, a key it owns, or — for an admin — any existing key).
def hook_body(uniq: Callable[[str], str], *, execution_key: str) -> dict[str, Any]:
    return {
        "name": uniq("hook"),
        "topic": uniq("t").replace("_", "-"),
        "tool": "e2e_echo",
        "tool_kwargs": {},
        "execution_key": execution_key,
    }


async def create_role(
    admin: ApiClient,
    *,
    name: str,
    base_tier: str,
    grants: dict[str, str],
    description: str = "",
) -> dict[str, Any]:
    """Create an operator-authored role over ``POST /api/auth/roles`` (admin-only,
    ``action=fenced``). Returns the persisted role body (``{name, base_tier, grants,
    scopes, ...}``). The base-tier jq is resolved server-side from ``base_tier`` — only
    ``editor``/``viewer`` are accepted (``admin`` is reserved)."""
    return await admin.post(
        "/api/auth/roles",
        json={"name": name, "base_tier": base_tier, "grants": grants, "description": description},
    )


async def update_role_grants(admin: ApiClient, name: str, grants: dict[str, str]) -> dict[str, Any]:
    """Edit a role's per-tag grant map LIVE over ``PUT /api/auth/roles/{name}`` — the
    version bump busts the grant cache so every holder's reach changes on their next
    request, no reassignment. ``base_tier`` is seed-fixed and left untouched."""
    return await admin.put(f"/api/auth/roles/{name}", json={"grants": grants})


async def create_user_with_role(
    stack: TaiStack, admin: ApiClient, uniq: Callable[[str], str], *, role: str
) -> tuple[str, str]:
    """Create an accounts user holding ``role`` and accept its invite into a live
    session. Returns ``(user_id, session_token)``. The role must already exist (the
    accounts create-user path applies it through ``apply_role``, which raises on an
    unknown role)."""
    created = await admin.post("/api/auth/users", json={"email": f"{uniq('rbac')}@e2e.test", "role": role})
    public = ApiClient(f"http://{stack.host}:{stack.port_a}")
    password = f"{uniq('pw')}-Aa1"
    accepted = await public.post(
        "/api/login/invite/accept",
        json={"invite_token": created["invite_token"], "password": password, "password_confirm": password},
    )
    return created["user_id"], accepted["token"]


async def mint_owned_key(
    admin: ApiClient, uniq: Callable[[str], str], *, owner_user_id: str, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Mint a machine key OWNED by ``owner_user_id``; return ``(raw sk- token, user_id)``.
    The user_id is the key's own id — the execution_key a holder binds. The owned key's
    reach is capped by the owner's CURRENT role live (keys inherit the owner's grant map)."""
    user_id = uniq("owned")
    created = await admin.post(
        "/api/auth/api-keys",
        json={
            "user_id": user_id,
            "description": "rbac owner-attenuated key",
            "scopes": scopes if scopes is not None else [OWNED_KEY_SCOPE],
            "owner_user_id": owner_user_id,
        },
    )
    return created["api_key"], user_id
