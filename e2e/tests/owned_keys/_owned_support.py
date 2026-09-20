"""Shared helpers for the owned-key suite: the two mint paths every key travels, plus
the catch-all scope the auth seed makes mintable.

Every api key belongs to a principal, so the suite provisions keys two ways against the
LIVE mint API (never seeded). From below: a non-admin HUMAN principal's accounts session
mints self-owned keys, subset-checked against the owner's scopes. By admin: the seeded
root mints keys owned by a SERVICE principal. Named ``_owned_support`` so its basename
never collides with another suite's helper module under prepend import mode."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.accounts_flow import invite_accept_login
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The catch-all scope the auth seed (``seed_bootstrap_key``) registers and maps every
# non-public route to; the root ``*`` policy satisfies it (and an owner may be narrowed to it).
SCOPE = "e2e-all"

# The password the provisioned human owner sets at invite-accept and logs in with.
_PASSWORD = "e2e-owner-password-1"


async def provision_human(
    stack: TaiStack, root: ApiClient, uniq: Callable[[str], str], *, role: str, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Provision a human principal holding ``role`` and log it in. Returns ``(user_id,
    session_token)``.

    The invite/accept/login idiom mints the principal's ``tai-sess-`` session. When
    ``scopes`` is given the admin narrows the principal's OWN policy row to exactly those
    scopes through the policy-edit door (the platform's attenuation authority) so a mint
    from below is subset-checked; ``None`` leaves the role's ``["*"]`` row."""
    email = f"{uniq('human')}@e2e.test"
    public = ApiClient(f"http://{stack.host}:{stack.port_a}")
    user_id, session = await invite_accept_login(root, public, email=email, role=role, password=_PASSWORD)
    if scopes is not None:
        await root.put(f"/api/auth/api-keys/{user_id}", json={"scopes": scopes})
    return user_id, session


async def provision_owner(
    stack: TaiStack, root: ApiClient, uniq: Callable[[str], str], *, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Provision a NON-ADMIN (editor) human owner — the from-below mint path — and return
    ``(owner_id, session_token)``. See :func:`provision_human` for ``scopes``."""
    return await provision_human(stack, root, uniq, role="editor", scopes=scopes)


async def provision_operator(stack: TaiStack, root: ApiClient, uniq: Callable[[str], str]) -> str:
    """The UNRESTRICTED operator: an ADMIN human session. A session is a top-level
    principal (its claims carry no owner claim), so the isolation read/answer/address gates
    give it the FULL view across every identity — the operator the suite proves sees
    everything. Returns the session token."""
    _op_id, session = await provision_human(stack, root, uniq, role="admin")
    return session


async def mint_owned(
    owner: ApiClient, uniq: Callable[[str], str], *, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Mint a key through an OWNER session (a mint from below). The server forces
    self-ownership and caps the scopes; the key can mint nothing. Returns ``(user_id, raw_token)``."""
    user_id = uniq("owned")
    created = await owner.post(
        "/api/auth/api-keys",
        json={"user_id": user_id, "description": "e2e owned key", "scopes": [SCOPE] if scopes is None else scopes},
    )
    raw = created["api_key"]
    assert raw.startswith("sk-")
    return user_id, raw


async def create_service_owner(root: ApiClient, uniq: Callable[[str], str], *, role: str = "editor") -> str:
    """Create a SERVICE principal (a keys-only owner that never logs in) and return its
    ``user_id``; keys minted for it attenuate under its ``role``."""
    created = await root.post(
        "/api/auth/principals",
        json={"kind": "service", "display_name": uniq("svc"), "role": role},
    )
    return created["user_id"]


async def mint_key_for(
    root: ApiClient, uniq: Callable[[str], str], owner_id: str, *, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Admin-mint a key OWNED by ``owner_id`` (the by-admin path). Returns ``(user_id, raw_token)``."""
    user_id = uniq("owned")
    created = await root.post(
        "/api/auth/api-keys",
        json={
            "user_id": user_id,
            "description": "e2e key for a service owner",
            "scopes": [SCOPE] if scopes is None else scopes,
            "owner_user_id": owner_id,
        },
    )
    raw = created["api_key"]
    assert raw.startswith("sk-")
    return user_id, raw


async def mint_admin_key(
    root: ApiClient, uniq: Callable[[str], str], *, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Admin-mint a key with NO explicit owner, so it belongs to the admin's own principal.
    The admin's ``*`` row leaves the key's effective scopes equal to its own. Returns ``(user_id, raw_token)``."""
    user_id = uniq("admin-owned")
    created = await root.post(
        "/api/auth/api-keys",
        json={
            "user_id": user_id,
            "description": "e2e admin-owned key",
            "scopes": [SCOPE] if scopes is None else scopes,
        },
    )
    raw = created["api_key"]
    assert raw.startswith("sk-")
    return user_id, raw


async def two_service_identities(stack: TaiStack, uniq: Callable[[str], str]) -> tuple[str, str, str, str]:
    """Two service owners, one admin-minted key each. Returns each key's OWN id (its
    isolation identity — its ``user_id``, NEVER its owner's): ``(a_id, a_raw, b_id, b_raw)``."""
    root = stack.api(port=stack.port_a)
    owner_a = await create_service_owner(root, uniq)
    owned_a_id, owned_a_raw = await mint_key_for(root, uniq, owner_a)
    owner_b = await create_service_owner(root, uniq)
    owned_b_id, owned_b_raw = await mint_key_for(root, uniq, owner_b)
    return owned_a_id, owned_a_raw, owned_b_id, owned_b_raw
