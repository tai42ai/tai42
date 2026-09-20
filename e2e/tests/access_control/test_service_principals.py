"""C-access-control — the service-principal lifecycle over the live deployment.

Every api key belongs to a principal. An admin creates a ``service`` principal, mints a key
owned by it, and the principal's ``disabled`` state gates that key at enforcement time:
disabling the principal denies its key on every worker, re-enabling restores it, and
deleting the principal removes the key. A non-admin owned key may create no principal.

Driven over the seeded accounts stack as the seeded root ``*`` admin (``stack.api()``).
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack

# The catch-all scope the auth seed makes mintable (a catch-all route resolves every
# non-public path to it, and the owner's ``*`` policy satisfies it).
_SCOPE = "e2e-all"


async def test_service_principal_key_lifecycle(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    admin = accounts_stack.api(port=accounts_stack.port_a)
    admin_id = (await admin.get("/api/auth/me"))["user_id"]

    # An admin creates a service principal; the door records the acting admin as its creator.
    svc_id = uniq("relay")
    created = await admin.post(
        "/api/auth/principals",
        json={"user_id": svc_id, "kind": "service", "display_name": "relay", "role": "editor"},
    )
    assert created["kind"] == "service", created
    assert created["created_by"] == admin_id, created

    # A key owned by the service principal authenticates and projects its service principal.
    key_id = f"{svc_id}-key"
    minted = await admin.post(
        "/api/auth/api-keys",
        json={"user_id": key_id, "description": "relay key", "scopes": [_SCOPE], "owner_user_id": svc_id},
    )
    assert minted["api_key"].startswith("sk-"), minted
    svc_key = accounts_stack.api(port=accounts_stack.port_a).with_token(minted["api_key"])
    me = await svc_key.get("/api/auth/me")
    assert me["principal"]["kind"] == "service", me
    assert me["owner_user_id"] == svc_id, me

    # Disabling the principal denies its key at enforcement (the disabled marker is read from
    # the owner's policy on every request).
    await admin.put(f"/api/auth/principals/{svc_id}", json={"disabled": True})
    denied = await svc_key.request_raw("GET", "/api/auth/me")
    assert denied.status_code in (401, 403), f"a disabled principal's key must be denied: {denied.status_code}"

    # Re-enabling restores the key.
    await admin.put(f"/api/auth/principals/{svc_id}", json={"disabled": False})
    restored = await svc_key.request_raw("GET", "/api/auth/me")
    assert restored.status_code == 200, f"a re-enabled principal's key must authenticate: {restored.status_code}"

    # Deleting the principal revokes its key and drops it from both listings.
    await admin.request_raw("DELETE", f"/api/auth/principals/{svc_id}")
    gone = await svc_key.request_raw("GET", "/api/auth/me")
    assert gone.status_code == 401, f"a deleted principal's key must 401: {gone.status_code}"
    principals = await admin.get("/api/auth/principals")
    assert all(p["user_id"] != svc_id for p in principals), principals
    tokens = await admin.get("/api/auth/tokens-payload")
    assert all(t["user_id"] != key_id for t in tokens), tokens


async def test_non_admin_owned_key_may_not_create_a_principal(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    admin = accounts_stack.api(port=accounts_stack.port_a)
    # An owned key minted under the admin's own principal is a non-admin leaf credential.
    minted = await admin.post(
        "/api/auth/api-keys",
        json={"user_id": uniq("leaf"), "description": "leaf key", "scopes": [_SCOPE]},
    )
    leaf = accounts_stack.api(port=accounts_stack.port_a).with_token(minted["api_key"])
    refused = await leaf.request_raw(
        "POST",
        "/api/auth/principals",
        json={"user_id": uniq("svc"), "kind": "service", "display_name": "relay", "role": "editor"},
    )
    assert refused.status_code == 403, f"a non-admin may not create a principal: {refused.status_code} {refused.text}"
