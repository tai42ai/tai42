"""C-accounts — the generic member-actions doors against a real accounts provider.

An admin lists the declared member-action catalog, invokes one, and a non-admin is refused.
The catalog door renders every declared action's
label to a plain string from the real provider's declarations; the invoke door's happy path mints
a one-time invite link (surfaced only on the result) and its typed-error path maps a provider
conflict to a 409; both doors refuse a non-admin caller."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:identity", "kind:accounts:postgres", "topology:replicas")

_PASSWORD = "member-actions-user-password-1"

_SCOPES = {"page", "member_row", "invite_row"}


async def test_catalog_renders_invoke_happy_and_error_and_admin_only(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # the seeded root sk- key (an unconditional "*" admin)

    # (1) The CATALOG door renders every declared action's label to a plain, non-empty
    # string from the real provider's declarations.
    catalog = await admin.get("/api/auth/member-actions")
    actions = catalog["actions"]
    assert actions, f"the accounts provider must declare member actions: {catalog}"
    for descriptor in actions:
        assert isinstance(descriptor["label"], str), descriptor
        assert descriptor["label"], f"every action label must render to a non-empty string: {descriptor}"
        assert isinstance(descriptor["key"], str), descriptor
        assert descriptor["key"], descriptor
        assert descriptor["scope"] in _SCOPES, descriptor
        assert isinstance(descriptor["input_schema"], dict), descriptor
        assert isinstance(descriptor["result_schema"], dict), descriptor

    catalog_keys = {d["key"] for d in actions}
    # The page-scoped action creates a new account; identify it by its generic scope,
    # never by a provider label.
    page_actions = [d for d in actions if d["scope"] == "page"]
    assert len(page_actions) == 1, f"exactly one page-scoped action is expected: {page_actions}"
    invite_key = page_actions[0]["key"]

    # (2a) INVOKE happy path: invite a member — the one-time link surfaces only in the result.
    email = f"{uniq('member')}@e2e.test"
    invoked = await admin.post(
        "/api/auth/member-actions/invoke",
        json={"action_key": invite_key, "target_handle": None, "input": {"email": email, "role": "editor"}},
    )
    result = invoked["result"]
    invite_token = result["invite_token"]
    assert result["login_path"].startswith("/login?invite="), invoked

    # The invited person surfaces in the directory as an invite row whose opaque tokens
    # join the catalog — the row carrier renders end to end without a 500.
    directory = await admin.get("/api/auth/members")
    invite_row = next((row for row in directory["invites"] if row["email"] == email), None)
    assert invite_row is not None, f"the invited email must appear as an invite row: {directory}"
    assert isinstance(invite_row["handle"], str), invite_row
    assert invite_row["handle"], invite_row
    assert invite_row["action_keys"], invite_row
    assert set(invite_row["action_keys"]) <= catalog_keys, (
        f"every row action key must resolve in the catalog: {invite_row['action_keys']} vs {catalog_keys}"
    )

    # The minted invite works: accepting it sets a password and mints the member's session.
    public = ApiClient(f"http://{stack.host}:{stack.port_a}")
    accepted = await public.post(
        "/api/login/invite/accept",
        json={"invite_token": invite_token, "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    session = accepted["token"]
    assert session.startswith("tai-sess-"), accepted

    # (2b) INVOKE typed-error path: inviting the SAME email again is a provider conflict,
    # mapped to a 409 — never a generic 500.
    conflict = await admin.request_raw(
        "POST",
        "/api/auth/member-actions/invoke",
        json={"action_key": invite_key, "target_handle": None, "input": {"email": email, "role": "editor"}},
    )
    assert conflict.status_code == 409, f"a duplicate invite must map to 409: {conflict.status_code} {conflict.text}"

    # (3) ADMIN-ONLY: the invited editor is a non-admin; both doors refuse it.
    member = stack.api(port=stack.port_b).with_token(session)
    denied_list = await member.request_raw("GET", "/api/auth/member-actions")
    assert denied_list.status_code == 403, (
        f"a non-admin must not list member actions: {denied_list.status_code} {denied_list.text}"
    )
    denied_invoke = await member.request_raw(
        "POST",
        "/api/auth/member-actions/invoke",
        json={
            "action_key": invite_key,
            "target_handle": None,
            "input": {"email": f"{uniq('x')}@e2e.test", "role": "viewer"},
        },
    )
    assert denied_invoke.status_code == 403, (
        f"a non-admin must not invoke a member action: {denied_invoke.status_code} {denied_invoke.text}"
    )
