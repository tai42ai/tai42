"""The provider's member-admin actions: declaration, routing, guards, and compensation.

Drives :meth:`PostgresAccountsProvider.member_actions` and ``invoke_member_action`` against
the in-memory store + services fakes, so the logic the removed admin routes once carried is
proven through the generic seam — including the last-enabled-admin guards and the
create-invite compensation. A correctable failure is raised as the matching contract
member-action error, which the skeleton maps to a status.
"""

from __future__ import annotations

import pytest
from tai42_contract.accounts.errors import (
    MemberActionBadRequestError,
    MemberActionConflictError,
    MemberActionNotFoundError,
)

from tai42_accounts_postgres import service
from tai42_accounts_postgres.member_actions import (
    CANCEL_INVITE,
    INVITE_ROW_ACTIONS,
    INVITE_USER,
    MEMBER_ROW_ACTIONS,
    REMOVE_MEMBER,
    RESEND_INVITE,
    UPDATE_MEMBER,
    InviteLinkResult,
    InviteUserInput,
    NoInput,
    NoResult,
    UpdateMemberInput,
)
from tai42_accounts_postgres.provider import PostgresAccountsProvider

from .conftest import future


def _provider(wire) -> PostgresAccountsProvider:
    return PostgresAccountsProvider(wire.settings)


def _add_user(wire, user_id, *, email=None, role="viewer", disabled=False, password_hash=None):
    wire.users.rows[user_id] = {
        "user_id": user_id,
        "email": email or f"{user_id}@x.y",
        "password_hash": password_hash,
        "role": role,
        "disabled": disabled,
        "created_at": future(0),
    }


# -- declaration ----------------------------------------------------------------


def test_member_actions_declares_this_providers_actions(wire):
    actions = {a.id: a for a in _provider(wire).member_actions()}
    assert set(actions) == {INVITE_USER, RESEND_INVITE, CANCEL_INVITE, UPDATE_MEMBER, REMOVE_MEMBER}

    assert actions[INVITE_USER].scope == "page"
    assert actions[INVITE_USER].input_model is InviteUserInput
    assert actions[INVITE_USER].result_model is InviteLinkResult
    assert actions[INVITE_USER].destructive is False
    assert actions[INVITE_USER].label.content == "Invite a user"

    assert actions[RESEND_INVITE].scope == "invite_row"
    assert actions[RESEND_INVITE].result_model is InviteLinkResult

    assert actions[CANCEL_INVITE].scope == "invite_row"
    assert actions[CANCEL_INVITE].destructive is True

    assert actions[UPDATE_MEMBER].scope == "member_row"
    assert actions[UPDATE_MEMBER].input_model is UpdateMemberInput

    assert actions[REMOVE_MEMBER].scope == "member_row"
    assert actions[REMOVE_MEMBER].destructive is True


# -- invite_user ----------------------------------------------------------------


async def test_invite_user_mints_link_and_creates_principal(wire):
    result = await _provider(wire).invoke_member_action(
        INVITE_USER, target=None, payload=InviteUserInput(email="New@X.Y", role="viewer")
    )
    assert isinstance(result, InviteLinkResult)
    assert result.invite_token.startswith("tai-inv-")
    assert result.login_path == f"/login?invite={result.invite_token}"
    # The human principal is created through the injected seam (row + role), keyed on the
    # normalized email as its display name; created_by is the (unset) admin caller.
    (user_id,) = list(wire.users.rows)
    assert ("create_principal", user_id, "human", "new@x.y", None, "viewer") in wire.admin.calls
    # The login row is NULL-password (a pending invite) and mirrors the role.
    assert wire.users.rows[user_id]["email"] == "new@x.y"
    assert wire.users.rows[user_id]["password_hash"] is None
    assert wire.users.rows[user_id]["role"] == "viewer"
    assert service.token_hash(result.invite_token) in wire.invites.rows


async def test_invite_user_email_taken_conflict(wire):
    _add_user(wire, "usr-1", email="dup@x.y")
    with pytest.raises(MemberActionConflictError, match="email already registered"):
        await _provider(wire).invoke_member_action(
            INVITE_USER, target=None, payload=InviteUserInput(email="dup@x.y", role="viewer")
        )
    # The compensation removed the just-created principal; only the pre-seeded row remains.
    assert set(wire.users.rows) == {"usr-1"}


async def test_invite_user_unknown_role_bad_request_and_compensates(wire):
    wire.admin.known_roles = {"admin", "editor", "viewer"}
    with pytest.raises(MemberActionBadRequestError, match="unknown role"):
        await _provider(wire).invoke_member_action(
            INVITE_USER, target=None, payload=InviteUserInput(email="new@x.y", role="bogus")
        )
    assert wire.users.rows == {}
    assert wire.invites.rows == {}


async def test_invite_user_apply_role_failure_compensates(wire):
    wire.admin.fail_apply_role = True
    with pytest.raises(RuntimeError, match="apply_role boom"):
        await _provider(wire).invoke_member_action(
            INVITE_USER, target=None, payload=InviteUserInput(email="new@x.y", role="viewer")
        )
    assert wire.users.rows == {}
    assert wire.invites.rows == {}


async def test_invite_user_invite_mint_failure_compensates(wire, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("invite boom")

    monkeypatch.setattr(wire.invites, "create", boom)
    with pytest.raises(RuntimeError, match="invite boom"):
        await _provider(wire).invoke_member_action(
            INVITE_USER, target=None, payload=InviteUserInput(email="new@x.y", role="viewer")
        )
    # The half-created user and its login row were dropped — re-runnable.
    assert wire.users.rows == {}


# -- update_member --------------------------------------------------------------


async def test_update_member_role_applies_template(wire):
    _add_user(wire, "usr-1", role="admin")  # another admin so the guard passes
    _add_user(wire, "usr-2", role="viewer")
    result = await _provider(wire).invoke_member_action(
        UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="admin")
    )
    assert isinstance(result, NoResult)
    assert ("apply_role", "usr-2", "admin") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "admin"


async def test_update_member_unknown_role_bad_request(wire):
    wire.admin.known_roles = {"admin", "editor", "viewer"}
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="viewer")
    with pytest.raises(MemberActionBadRequestError, match="unknown role"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="bogus")
        )
    assert wire.users.rows["usr-2"]["role"] == "viewer"


async def test_update_member_demote_allowed_when_another_admin_remains(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="viewer"))
    assert ("apply_role", "usr-2", "viewer") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "viewer"


async def test_update_member_disable_non_admin_needs_no_guard(wire):
    _add_user(wire, "usr-2", role="viewer")
    wire.sessions.rows["th"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(disabled=True))
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert "th" not in wire.sessions.rows
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_update_member_disable_kills_credentials_first(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")
    wire.sessions.rows["th-2"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(disabled=True))
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert "th-2" not in wire.sessions.rows
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_update_member_reenable_reverses_order(wire):
    _add_user(wire, "usr-2", role="viewer", disabled=True)
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(disabled=False))
    assert wire.users.rows["usr-2"]["disabled"] is False
    assert ("set_user_disabled", "usr-2", "False") in wire.admin.calls


async def test_update_member_combined_demote_and_reenable_applies_both(wire):
    _add_user(wire, "usr-1", role="admin")  # the surviving admin
    _add_user(wire, "usr-2", role="admin", disabled=True)
    await _provider(wire).invoke_member_action(
        UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="user", disabled=False)
    )
    assert ("apply_role", "usr-2", "user") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "user"
    assert ("set_user_disabled", "usr-2", "False") in wire.admin.calls
    assert wire.users.rows["usr-2"]["disabled"] is False


async def test_update_member_combined_role_and_disable_applies_both(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")
    await _provider(wire).invoke_member_action(
        UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="user", disabled=True)
    )
    assert ("apply_role", "usr-2", "user") in wire.admin.calls
    assert wire.users.rows["usr-2"]["role"] == "user"
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_update_member_cannot_disable_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(disabled=True)
        )


async def test_update_member_cannot_demote_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(role="viewer")
        )


async def test_update_member_concurrent_removals_cannot_both_orphan(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")

    def _other_disables_usr2(store) -> None:
        store.rows["usr-2"]["disabled"] = True

    wire.users.admin_guard_hook = _other_disables_usr2
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(disabled=True)
        )
    assert wire.users.rows["usr-1"]["disabled"] is False


async def test_update_member_demote_reevaluates_disabled_under_lock(wire):
    _add_user(wire, "usr-1", role="admin", disabled=True)
    _add_user(wire, "usr-2", role="admin")

    def _reenable_usr1_disable_usr2(store) -> None:
        store.rows["usr-1"]["disabled"] = False
        store.rows["usr-2"]["disabled"] = True

    wire.users.admin_guard_hook = _reenable_usr1_disable_usr2
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(role="viewer")
        )
    assert wire.users.rows["usr-1"]["role"] == "admin"


async def test_update_member_target_vanishes_under_lock_not_found(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="viewer")

    def _delete_usr2(store) -> None:
        store.rows.pop("usr-2", None)

    wire.users.admin_guard_hook = _delete_usr2
    with pytest.raises(MemberActionNotFoundError, match="user not found"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="admin")
        )


async def test_update_member_unknown_user_not_found(wire):
    with pytest.raises(MemberActionNotFoundError, match="user not found"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="ghost", payload=UpdateMemberInput(role="viewer")
        )


async def test_update_member_requires_target(wire):
    with pytest.raises(MemberActionBadRequestError, match="requires a target"):
        await _provider(wire).invoke_member_action(UPDATE_MEMBER, target=None, payload=UpdateMemberInput(role="viewer"))


# -- remove_member / cancel_invite ----------------------------------------------


async def test_remove_member_orders_policy_first(wire):
    _add_user(wire, "usr-1", role="admin")  # keeps an admin alive
    _add_user(wire, "usr-2", role="viewer", password_hash="h")
    wire.sessions.rows["th"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    wire.invites.rows["ih"] = {"user_id": "usr-2", "expires_at": future(), "consumed_at": None}
    result = await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-2", payload=NoInput())
    assert isinstance(result, NoResult)
    assert ("remove_policy", "usr-2") in wire.admin.calls
    assert "usr-2" not in wire.users.rows
    assert wire.sessions.rows == {}
    assert wire.invites.rows == {}


async def test_remove_member_admin_allowed_when_another_admin_remains(wire):
    _add_user(wire, "usr-1", role="admin", password_hash="h")
    _add_user(wire, "usr-2", role="admin")  # the surviving admin
    await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-1", payload=NoInput())
    assert ("remove_policy", "usr-1") in wire.admin.calls
    assert "usr-1" not in wire.users.rows


async def test_remove_member_cannot_delete_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-1", payload=NoInput())
    assert "usr-1" in wire.users.rows


async def test_remove_member_concurrent_admin_delete_refused_when_other_removed(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")

    def _other_demotes_usr2(store) -> None:
        store.rows["usr-2"]["role"] = "viewer"

    wire.users.admin_guard_hook = _other_demotes_usr2
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-1", payload=NoInput())
    assert "usr-1" in wire.users.rows


async def test_remove_member_unknown_not_found(wire):
    with pytest.raises(MemberActionNotFoundError, match="user not found"):
        await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="ghost", payload=NoInput())


async def test_remove_member_requires_target(wire):
    with pytest.raises(MemberActionBadRequestError, match="requires a target"):
        await _provider(wire).invoke_member_action(REMOVE_MEMBER, target=None, payload=NoInput())


async def test_cancel_invite_deletes_the_invited_user(wire):
    _add_user(wire, "usr-1", role="admin")  # keeps an admin alive
    _add_user(wire, "usr-pending", role="viewer", password_hash=None)
    wire.invites.rows["ih"] = {"user_id": "usr-pending", "expires_at": future(), "consumed_at": None}
    await _provider(wire).invoke_member_action(CANCEL_INVITE, target="usr-pending", payload=NoInput())
    assert "usr-pending" not in wire.users.rows
    assert wire.invites.rows == {}


# -- resend_invite --------------------------------------------------------------


async def test_resend_invite_for_pending_user(wire):
    _add_user(wire, "usr-2", role="viewer", password_hash=None)
    result = await _provider(wire).invoke_member_action(RESEND_INVITE, target="usr-2", payload=NoInput())
    assert isinstance(result, InviteLinkResult)
    assert result.invite_token.startswith("tai-inv-")
    assert service.token_hash(result.invite_token) in wire.invites.rows


async def test_resend_invite_conflict_when_password_set(wire):
    _add_user(wire, "usr-2", role="viewer", password_hash="h")
    with pytest.raises(MemberActionConflictError, match="already has a password"):
        await _provider(wire).invoke_member_action(RESEND_INVITE, target="usr-2", payload=NoInput())


async def test_resend_invite_unknown_not_found(wire):
    with pytest.raises(MemberActionNotFoundError, match="user not found"):
        await _provider(wire).invoke_member_action(RESEND_INVITE, target="ghost", payload=NoInput())


async def test_resend_invite_requires_target(wire):
    with pytest.raises(MemberActionBadRequestError, match="requires a target"):
        await _provider(wire).invoke_member_action(RESEND_INVITE, target=None, payload=NoInput())


# -- unknown action id ----------------------------------------------------------


async def test_invoke_unknown_action_not_found(wire):
    with pytest.raises(MemberActionNotFoundError, match="unknown member action"):
        await _provider(wire).invoke_member_action("nope", target=None, payload=NoInput())


# -- the per-row applicable action ids reported by list_members -----------------


async def test_list_members_rows_report_their_applicable_actions(wire):
    _add_user(wire, "usr-active", email="a@x.test", password_hash="hash", role="admin")
    _add_user(wire, "usr-pending", email="p@x.test", password_hash=None, role="viewer")
    wire.invites.rows["th-1"] = {"user_id": "usr-pending", "expires_at": future(3600), "consumed_at": None}

    listing = await _provider(wire).list_members()

    (member,) = listing.members
    assert member.principal_ids == ["usr-active"]
    assert member.actions == list(MEMBER_ROW_ACTIONS)

    (invite,) = listing.invites
    assert invite.actions == list(INVITE_ROW_ACTIONS)
