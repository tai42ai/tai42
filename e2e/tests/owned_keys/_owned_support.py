"""Shared helpers for the owned-key suite: mint the owner→owned key chain through
the LIVE mint API (never seeded), plus the catch-all scope the auth bootstrap makes
mintable.

Every key here is provisioned by POSTing ``/api/auth/api-keys`` so the tests exercise
the real mint + attenuation rules as their fixture path — a seed-side owned key would
bypass the very surface under test. Named ``_owned_support`` (not ``_support``) so its
module basename never collides with another suite's helper module under pytest's
prepend import mode.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient

# The catch-all scope the auth bootstrap seeds (``seed_bootstrap_key``): every
# non-public route resolves to it, and the root ``*`` policy satisfies it, so a key
# minted with it reaches the whole protected surface.
SCOPE = "e2e-all"


async def mint_owner(
    root: ApiClient, uniq: Callable[[str], str], *, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Mint an UNRESTRICTED owner key through the root client — no owner claim, so it
    can itself mint an owned key. Returns ``(user_id, raw_token)``."""
    user_id = uniq("owner")
    created = await root.post(
        "/api/auth/api-keys",
        json={"user_id": user_id, "description": "e2e owner key", "scopes": [SCOPE] if scopes is None else scopes},
    )
    raw = created["api_key"]
    assert raw.startswith("sk-")
    return user_id, raw


async def mint_owned(
    owner: ApiClient, uniq: Callable[[str], str], *, scopes: list[str] | None = None
) -> tuple[str, str]:
    """Mint a RESTRICTED owned key through an OWNER client — the server forces
    self-ownership, so the minted key carries the owner's identity as its owner claim
    and can mint nothing. Returns ``(user_id, raw_token)``."""
    user_id = uniq("owned")
    created = await owner.post(
        "/api/auth/api-keys",
        json={"user_id": user_id, "description": "e2e owned key", "scopes": [SCOPE] if scopes is None else scopes},
    )
    raw = created["api_key"]
    assert raw.startswith("sk-")
    return user_id, raw
