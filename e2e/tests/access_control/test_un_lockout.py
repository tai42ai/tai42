"""M20 F1 — the un-lockout guards: the reserved ``admin`` role is permanent, an assigned
role cannot be deleted, and the LAST enabled admin can never be removed.

Two fences keep a deployment from locking itself out of the control plane:
- the skeleton's reserved-role guard (create/edit/delete of the permanent ``admin``
  role is refused; a still-assigned role cannot be deleted);
- the accounts last-admin guard (with two admins either may be removed, but the LAST
  enabled admin cannot be demoted, disabled, or deleted).

AS-BUILT reconciliation: the reserved-``admin`` guards raise ``ForbiddenError`` → **403**
(not the plan's 409); delete-of-assigned and every last-admin guard raise
``ConflictError``/return **409**. There is no ``rename`` route to guard (a rename is a
create-new + delete-old, each already fenced). The seeded root ``sk-`` key is a
super-admin discriminator, NOT an accounts user, so it never counts toward the accounts
admin population the last-admin guard reads.
"""

from __future__ import annotations

from collections.abc import Callable

from _rbac_support import create_role, create_user_with_role

from tai42_e2e.stack import TaiStack


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
    removed = await admin.request_raw("DELETE", f"/api/auth/users/{holder_id}")
    assert removed.status_code == 200, f"removing the holder must succeed: {removed.status_code} {removed.text}"
    now_deletable = await admin.request_raw("DELETE", f"/api/auth/roles/{assigned_role}")
    assert now_deletable.status_code == 200, (
        f"an unassigned role must delete cleanly: {now_deletable.status_code} {now_deletable.text}"
    )


async def test_multiple_admins_and_last_admin_guard(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """Two accounts admins may coexist and either may be removed; the LAST enabled admin
    cannot be demoted, disabled, or deleted. A freshly created user is enabled, so the
    created admins count immediately — no invite acceptance needed."""
    admin = accounts_stack.api(port=accounts_stack.port_a)

    # Two admin holders (the accounts admin population starts empty — the seeded root key
    # is not an accounts user).
    a = await admin.post("/api/auth/users", json={"email": f"{uniq('admin-a')}@e2e.test", "role": "admin"})
    b = await admin.post("/api/auth/users", json={"email": f"{uniq('admin-b')}@e2e.test", "role": "admin"})
    a_id, b_id = a["user_id"], b["user_id"]

    # With two admins, demoting one is ALLOWED (the other remains).
    demote_a = await admin.request_raw("PUT", f"/api/auth/users/{a_id}", json={"role": "viewer"})
    assert demote_a.status_code == 200, (
        f"demoting one of two admins must be allowed: {demote_a.status_code} {demote_a.text}"
    )

    # B is now the last enabled admin: demote / disable / delete are each refused (409).
    demote_last = await admin.request_raw("PUT", f"/api/auth/users/{b_id}", json={"role": "viewer"})
    assert demote_last.status_code == 409, (
        f"demoting the last enabled admin must be refused: {demote_last.status_code} {demote_last.text}"
    )
    disable_last = await admin.request_raw("PUT", f"/api/auth/users/{b_id}", json={"disabled": True})
    assert disable_last.status_code == 409, (
        f"disabling the last enabled admin must be refused: {disable_last.status_code} {disable_last.text}"
    )
    delete_last = await admin.request_raw("DELETE", f"/api/auth/users/{b_id}")
    assert delete_last.status_code == 409, (
        f"deleting the last enabled admin must be refused: {delete_last.status_code} {delete_last.text}"
    )
