"""The un-lockout guards: the reserved ``admin`` role is permanent, an assigned
role cannot be deleted, and the LAST enabled admin principal can never be removed.

Two fences keep a deployment from locking itself out of the control plane:
- the reserved-role guard (create/edit/delete of the permanent ``admin`` role is refused
  with 403; a still-assigned role cannot be deleted, 409);
- the one last-admin guard: "admin" is every enabled principal whose own policy is
  admin-shaped — an accounts member holding the ``admin`` role and the seeded keys-only
  ``e2e-owner`` alike — and no door (the member actions, the principals door, the key
  door) may leave none enabled (409, nothing written).

A principal's ``disabled`` state is changed only through the principals door or a member
action: the key door refuses a ``disabled`` value that is not exactly that state (400) and
carries the stored one across any other policy edit, and the key mint refuses any supplied
``disabled`` (400).

There is no ``rename`` route to guard (a rename is a create-new + delete-old, each
already fenced).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.member_admin import handle_for_user, invite_member, remove_member_raw, update_member_raw
from tai42_e2e.stack import TaiStack

from ._rbac_support import create_role, create_user_with_role

pytestmark = pytest.mark.needs("kind:identity", "kind:accounts:postgres")


async def test_reserved_admin_role_and_assigned_role_guards(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    admin = accounts_stack.api(port=accounts_stack.port_a)

    # The reserved permanent ``admin`` role: create / edit / delete all refused (403),
    # even for the admin caller — it is un-createable, un-editable (block-downgrade), and
    # undeletable by construction.
    creating = await admin.request_raw(
        "POST", "/api/auth/roles", json={"name": "admin", "base_tier": "editor", "grants": {}}
    )
    assert creating.status_code == 403, (
        f"creating the reserved admin role must be refused: {creating.status_code} {creating.text}"
    )
    editing = await admin.request_raw("PUT", "/api/auth/roles/admin", json={"grants": {"hooks": "read"}})
    assert editing.status_code == 403, (
        f"editing the reserved admin role must be refused (block-downgrade): {editing.status_code} {editing.text}"
    )
    deleting = await admin.request_raw("DELETE", "/api/auth/roles/admin")
    assert deleting.status_code == 403, (
        f"deleting the reserved admin role must be refused: {deleting.status_code} {deleting.text}"
    )

    # A name collision on a custom role is a loud 409.
    role_name = uniq("dup")
    await create_role(admin, name=role_name, base_tier="editor", grants={"hooks": "read"})
    collision = await admin.request_raw(
        "POST", "/api/auth/roles", json={"name": role_name, "base_tier": "editor", "grants": {}}
    )
    assert collision.status_code == 409, (
        f"a duplicate role name must be refused (409): {collision.status_code} {collision.text}"
    )

    # A role still ASSIGNED to a principal cannot be deleted (409) — a holder can never be
    # orphaned. Once the holder is removed, the same delete succeeds.
    assigned_role = uniq("assigned")
    await create_role(admin, name=assigned_role, base_tier="editor", grants={"presets": "read"})
    holder_id, _session = await create_user_with_role(accounts_stack, admin, uniq, role=assigned_role)
    while_assigned = await admin.request_raw("DELETE", f"/api/auth/roles/{assigned_role}")
    assert while_assigned.status_code == 409, (
        f"deleting an assigned role must be refused (409): {while_assigned.status_code} {while_assigned.text}"
    )

    # Remove the holder (its policy + role pointer), then the role deletes cleanly.
    holder_handle = await handle_for_user(admin, holder_id)
    removed = await remove_member_raw(admin, holder_handle)
    assert removed.status_code == 200, f"removing the holder must succeed: {removed.status_code} {removed.text}"
    now_deletable = await admin.request_raw("DELETE", f"/api/auth/roles/{assigned_role}")
    assert now_deletable.status_code == 200, (
        f"an unassigned role must delete cleanly: {now_deletable.status_code} {now_deletable.text}"
    )


# The keys-only owner the stack is seeded with: an admin principal outside the accounts table.
_SEEDED_OWNER = "e2e-owner"


async def _invite_admin_with_session(stack: TaiStack, admin: ApiClient, uniq: Callable[[str], str]) -> dict[str, str]:
    """Invite an accounts member holding ``admin`` and accept it into a live session."""
    invited = await invite_member(admin, email=f"{uniq('admin-a')}@e2e.test", role="admin")
    public = ApiClient(stack.origin(stack.port_a))
    password = f"{uniq('pw')}-Aa1"
    accepted = await public.post(
        "/api/login/invite/accept",
        json={"invite_token": invited["invite_token"], "password": password, "password_confirm": password},
    )
    return {"user_id": invited["user_id"], "handle": invited["handle"], "session": accepted["token"]}


def _refused(response: httpx.Response, what: str) -> None:
    assert response.status_code == 409, f"{what} must be refused (409): {response.status_code} {response.text}"
    assert "last enabled admin" in response.text, f"{what} must name the last-admin rule: {response.text}"


@pytest.mark.needs("mutable", "setting:the-seeded-owner-is-the-only-admin")
async def test_one_last_admin_guard_counts_every_admin_principal(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """Every enabled admin principal counts, whatever door changes it.

    A, an invited accounts admin, may disable the keys-only owner (A remains); then A is the
    last enabled admin and its own demote, disable and delete are refused. With the owner
    enabled again, demoting A — the last ACCOUNTS admin — is allowed, because the keys-only
    owner counts; the owner is then the last admin, and the principals door and the key door
    (an emptied policy) refuse to remove it; the universal scope can neither be deleted nor
    carry a route, so no scope cascade reaches an admin's grant. A policy edit never changes
    the owner's ``disabled`` state: clearing its policy data while it is disabled leaves it
    disabled, and writing a ``disabled`` marker through the key door is refused (400).
    """
    root = accounts_stack.api(port=accounts_stack.port_a)
    a = await _invite_admin_with_session(accounts_stack, root, uniq)
    as_a = ApiClient(accounts_stack.origin(accounts_stack.port_a), auth_token=a["session"])

    disabled_owner = await as_a.request_raw("PUT", f"/api/auth/principals/{_SEEDED_OWNER}", json={"disabled": True})
    assert disabled_owner.status_code == 200, (
        f"disabling the keys-only owner while A remains must be allowed: {disabled_owner.status_code} "
        f"{disabled_owner.text}"
    )
    try:
        # A policy edit that clears the disabled owner's policy data leaves it disabled on
        # every surface: its key is still denied and the principals listing still says so.
        cleared = await as_a.request_raw("PUT", f"/api/auth/api-keys/{_SEEDED_OWNER}", json={"policy_data": {}})
        assert cleared.status_code == 200, (
            f"clearing the disabled owner's policy data: {cleared.status_code} {cleared.text}"
        )
        owner_key = await root.request_raw("GET", "/api/auth/me")
        assert owner_key.status_code in (401, 403), (
            f"the disabled owner's key must stay denied after a policy edit: {owner_key.status_code} {owner_key.text}"
        )
        listed = {p["user_id"]: p for p in await as_a.get("/api/auth/principals")}
        assert listed[_SEEDED_OWNER]["disabled"] is True, listed[_SEEDED_OWNER]

        _refused(await update_member_raw(as_a, a["handle"], role="viewer"), "demoting the last enabled admin")
        _refused(await update_member_raw(as_a, a["handle"], disabled=True), "disabling the last enabled admin")
        _refused(await remove_member_raw(as_a, a["handle"]), "deleting the last enabled admin")
    finally:
        reenabled = await as_a.request_raw("PUT", f"/api/auth/principals/{_SEEDED_OWNER}", json={"disabled": False})
        assert reenabled.status_code == 200, f"re-enabling the owner: {reenabled.status_code} {reenabled.text}"

    demoted = await update_member_raw(root, a["handle"], role="viewer")
    assert demoted.status_code == 200, (
        f"demoting the last accounts admin must be allowed while the keys-only owner is enabled: "
        f"{demoted.status_code} {demoted.text}"
    )

    _refused(
        await root.request_raw("PUT", f"/api/auth/principals/{_SEEDED_OWNER}", json={"disabled": True}),
        "disabling the keys-only owner as the last admin",
    )
    _refused(
        await root.request_raw("DELETE", f"/api/auth/principals/{_SEEDED_OWNER}"),
        "deleting the keys-only owner as the last admin",
    )
    _refused(
        await root.request_raw("PUT", f"/api/auth/api-keys/{_SEEDED_OWNER}", json={"scopes": []}),
        "emptying the last admin's own policy through the key door",
    )
    marker = await root.request_raw(
        "PUT", f"/api/auth/api-keys/{_SEEDED_OWNER}", json={"policy_data": {"disabled": True}}
    )
    assert marker.status_code == 400, (
        f"writing a disabled marker through the key door must be refused (400): {marker.status_code} {marker.text}"
    )
    assert "not policy content" in marker.text, f"the refusal must say 'disabled' is not policy content: {marker.text}"
    loose = await root.request_raw(
        "PUT", f"/api/auth/api-keys/{_SEEDED_OWNER}", json={"policy_data": {"disabled": "true"}}
    )
    assert loose.status_code == 400, (
        f"a non-boolean disabled value through the key door must be refused (400): {loose.status_code} {loose.text}"
    )
    assert "not policy content" in loose.text, f"the refusal must say 'disabled' is not policy content: {loose.text}"
    marked_key = uniq("marked-key")
    minted = await root.request_raw(
        "POST",
        "/api/auth/api-keys",
        json={"user_id": marked_key, "description": "d", "scopes": [], "policy_data": {"disabled": True}},
    )
    assert minted.status_code == 400, (
        f"minting a key with a disabled claim must be refused (400): {minted.status_code} {minted.text}"
    )
    assert "not policy content" in minted.text, f"the refusal must say 'disabled' is not policy content: {minted.text}"
    listed = await root.get("/api/auth/tokens-payload")
    assert marked_key not in str(listed), f"a refused mint must write nothing: {listed}"
    universal = await root.request_raw("DELETE", "/api/auth/scopes/*")
    assert universal.status_code == 400, (
        f"deleting the universal scope must be refused (400): {universal.status_code} {universal.text}"
    )
    assert "universal grant" in universal.text, f"the refusal must name the universal grant: {universal.text}"
    star_route = await root.request_raw("POST", "/api/auth/scopes", json={"scope_id": "*", "url": f"/{uniq('star')}"})
    assert star_route.status_code == 400, (
        f"mapping a route to the universal scope must be refused (400): {star_route.status_code} {star_route.text}"
    )
    assert "universal grant" in star_route.text, f"the refusal must name the universal grant: {star_route.text}"

    removed = await remove_member_raw(root, a["handle"])
    assert removed.status_code == 200, f"removing the demoted member: {removed.status_code} {removed.text}"


async def test_a_version_1_accounts_archive_is_refused(accounts_stack: TaiStack) -> None:
    """An accounts archive of version 1 (it carries a per-user role) restores nothing and
    reports the section error naming the version."""
    root = accounts_stack.api(port=accounts_stack.port_a)
    archive = {
        "version": 1,
        "users": [
            {
                "user_id": "usr-archived",
                "email": "archived@e2e.test",
                "password_hash": None,
                "role": "admin",
                "disabled": False,
                "created_at": "2026-01-02T03:04:05+00:00",
            }
        ],
    }
    document = {"version": 1, "sections": {"accounts": archive}, "errors": {}}
    result = await root.post(
        "/api/backup/import", json={"document": document, "sections": ["accounts"]}, retry_on_reloading=True
    )
    assert result["ok"] is False, f"a version-1 accounts archive must not import ok: {result}"
    errors = result["sections"]["accounts"]["errors"]
    assert any("version 1" in str(error) for error in errors), f"the refusal must name version 1: {errors}"
    directory = await root.get("/api/auth/members")
    assert all(row["email"] != "archived@e2e.test" for row in [*directory["members"], *directory["invites"]])
