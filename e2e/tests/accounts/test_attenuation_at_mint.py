"""C-accounts — scope attenuation and admin-classification at key mint.

A non-admin caller may mint only keys whose scopes are a subset of its own; a
superset is refused with a 400 that names the offending scopes. Admin classification
is the owner's role/condition, never a key's scope breadth: an editor-minted
condition-free ``["*"]`` key is still non-admin on the key-management surface."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The one non-public scope the accounts stack maps every /api route onto (see the
# seeded route table): a real, mintable scope a limited-scope key can carry.
_SCOPE = "e2e-all"
_PASSWORD = "editor-user-password-1"


def _unauth(stack: TaiStack, port: int) -> ApiClient:
    return ApiClient(f"http://{stack.host}:{port}")


async def _register_star_scope(admin: ApiClient, uniq: Callable[[str], str]) -> None:
    """Map a dummy url onto the ``"*"`` scope so it EXISTS for minting.

    A key mint validates every requested scope against the scope→url table; the
    seeded table carries only ``e2e-all``, so ``"*"`` must be registered before an
    all-scopes key can be minted through the real provisioning surface."""
    await admin.post("/api/auth/scopes", json={"scope_id": "*", "url": f"/{uniq('star')}"})


async def _invited_session(stack: TaiStack, admin: ApiClient, uniq: Callable[[str], str], role: str) -> str:
    """Create a ``role`` user through the admin surface and accept its invite,
    returning a live session token for that non-admin account."""
    email = f"{uniq('user')}@e2e.test"
    invite = await admin.post("/api/auth/users", json={"email": email, "role": role})
    accepted = await _unauth(stack, stack.port_a).post(
        "/api/login/invite/accept",
        json={"invite_token": invite["invite_token"], "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    return accepted["token"]


async def test_non_admin_mint_is_capped_to_own_scopes(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key (unconditional "*" admin)

    # Admin mints an OWNERLESS machine key with a LIMITED scope set — the headless
    # path (no owner) stays alive, and its non-"*" scopes make it a non-admin minter.
    machine_raw = (
        await admin.post(
            "/api/auth/api-keys",
            json={"user_id": uniq("machine"), "description": "limited machine key", "scopes": [_SCOPE]},
        )
    )["api_key"]
    assert machine_raw.startswith("sk-"), machine_raw
    machine = stack.api(port=stack.port_b).with_token(machine_raw)

    # ⊆ own: a sub-key with the same limited scope is granted.
    sub_raw = (
        await machine.post(
            "/api/auth/api-keys",
            json={"user_id": uniq("sub"), "description": "attenuated sub key", "scopes": [_SCOPE]},
        )
    )["api_key"]
    assert sub_raw.startswith("sk-"), sub_raw

    # ⊄ own: a scope the caller does NOT hold is refused with a 400 naming it (400,
    # not 403 — 403 is reserved for owned-key / different-owner cases). The scope is
    # REGISTERED first (via the admin scope table) so the mint clears the
    # scope-existence pre-check and is refused ONLY by the attenuation (subset) rule —
    # an unregistered scope would 400 on existence alone, masking an attenuation
    # regression; a registered-but-unheld scope makes this assertion load-bearing.
    excess_scope = uniq("unheld")
    await admin.post("/api/auth/scopes", json={"scope_id": excess_scope, "url": f"/{uniq('u')}"})
    excess = await machine.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": uniq("sub"), "description": "over-broad", "scopes": [excess_scope]},
    )
    assert excess.status_code == 400, f"a superset mint must 400: {excess.status_code} {excess.text}"
    assert excess_scope in excess.text, f"the 400 must name the offending scope: {excess.text}"

    # Admin is unrestricted: it may mint an ownerless "*" key with no attenuation.
    await _register_star_scope(admin, uniq)
    admin_raw = (
        await admin.post(
            "/api/auth/api-keys",
            json={"user_id": uniq("adminkey"), "description": "ownerless star key", "scopes": ["*"]},
        )
    )["api_key"]
    assert admin_raw.startswith("sk-"), admin_raw


async def test_editor_star_key_is_non_admin_on_the_key_surface(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)

    # An admin-owned reference key the editor's key must NOT be able to touch.
    other_raw = (
        await admin.post(
            "/api/auth/api-keys",
            json={"user_id": uniq("other"), "description": "someone else's key", "scopes": [_SCOPE]},
        )
    )["api_key"]
    other_user_id = uniq("victim")
    await admin.post(
        "/api/auth/api-keys",
        json={"user_id": other_user_id, "description": "victim key", "scopes": [_SCOPE]},
    )
    assert other_raw.startswith("sk-"), other_raw

    # An editor session mints a condition-free ["*"] key owned by itself. Its ["*"]
    # scope does NOT make it admin — the owner claim classifies it non-admin.
    await _register_star_scope(admin, uniq)
    editor_session = await _invited_session(stack, admin, uniq, role="editor")
    editor = stack.api(port=stack.port_a).with_token(editor_session)
    star_key_raw = (
        await editor.post(
            "/api/auth/api-keys",
            json={"user_id": uniq("starkey"), "description": "editor star key", "scopes": ["*"]},
        )
    )["api_key"]
    assert star_key_raw.startswith("sk-"), star_key_raw
    star = stack.api(port=stack.port_b).with_token(star_key_raw)

    # NON-admin despite ["*"]: an owned key may mint nothing.
    minted = await star.request_raw(
        "POST", "/api/auth/api-keys", json={"user_id": uniq("x"), "description": "nope", "scopes": [_SCOPE]}
    )
    assert minted.status_code == 403, f"an owned ['*'] key must not mint: {minted.status_code} {minted.text}"

    # It cannot list other owners' keys — its listing is scoped to keys it owns (none).
    listing = await star.get("/api/auth/tokens-payload")
    assert listing == [], f"a non-admin key must see only its own keys, saw: {listing}"

    # It cannot revoke or edit a key it does not own.
    revoked = await star.request_raw("DELETE", f"/api/auth/api-keys/{other_user_id}")
    assert revoked.status_code == 403, f"a non-admin key must not revoke another's key: {revoked.status_code}"
    edited = await star.request_raw("PUT", f"/api/auth/api-keys/{other_user_id}", json={"description": "hijacked"})
    assert edited.status_code == 403, f"a non-admin key must not edit another's key: {edited.status_code}"
