"""The provider's member-admin actions: declaration, routing, guards, and compensation.

Drives :meth:`PostgresAccountsProvider.member_actions` and ``invoke_member_action`` against
the in-memory store + services fakes through the generic seam — including the mapping of
the platform's last-admin refusal and the create-invite compensation. A correctable failure
is raised as the matching contract member-action error, which the skeleton maps to a status.
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


def _add_user(wire, user_id, *, email=None, role: str | None = "viewer", disabled=False, password_hash=None):
    """A login row plus its platform principal (role and disabled state) in the services fake."""
    wire.users.rows[user_id] = {
        "user_id": user_id,
        "email": email or f"{user_id}@x.y",
        "password_hash": password_hash,
        "disabled": disabled,
        "created_at": future(0),
    }
    wire.admin.roles[user_id] = role
    if disabled:
        wire.admin.disabled.add(user_id)


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
    # The login row is NULL-password (a pending invite); the role lives on the principal.
    assert wire.users.rows[user_id]["email"] == "new@x.y"
    assert wire.users.rows[user_id]["password_hash"] is None
    assert wire.admin.roles[user_id] == "viewer"
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
    assert ("principal_roles", ("usr-2",)) in wire.admin.calls
    assert wire.admin.roles["usr-2"] == "admin"


async def test_update_member_same_role_writes_nothing(wire):
    _add_user(wire, "usr-2", role="viewer")
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="viewer"))
    assert not any(call[0] == "apply_role" for call in wire.admin.calls)


async def test_update_member_role_change_from_no_role(wire):
    _add_user(wire, "usr-2", role=None)
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="viewer"))
    assert ("apply_role", "usr-2", "viewer") in wire.admin.calls


async def test_update_member_without_a_principal_fails_loudly(wire):
    _add_user(wire, "usr-2", role="viewer")
    del wire.admin.roles["usr-2"]
    with pytest.raises(RuntimeError, match="has no access-control principal"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="editor")
        )


async def test_update_member_unknown_role_bad_request(wire):
    wire.admin.known_roles = {"admin", "editor", "viewer"}
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="viewer")
    with pytest.raises(MemberActionBadRequestError, match="unknown role"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="bogus")
        )
    assert wire.admin.roles["usr-2"] == "viewer"


async def test_update_member_demote_allowed_when_another_admin_remains(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="viewer"))
    assert ("apply_role", "usr-2", "viewer") in wire.admin.calls
    assert wire.admin.roles["usr-2"] == "viewer"


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
    assert wire.admin.roles["usr-2"] == "user"
    assert ("set_user_disabled", "usr-2", "False") in wire.admin.calls
    assert wire.users.rows["usr-2"]["disabled"] is False


async def test_update_member_combined_role_and_disable_applies_both(wire):
    _add_user(wire, "usr-1", role="admin")
    _add_user(wire, "usr-2", role="admin")
    await _provider(wire).invoke_member_action(
        UPDATE_MEMBER, target="usr-2", payload=UpdateMemberInput(role="user", disabled=True)
    )
    assert ("apply_role", "usr-2", "user") in wire.admin.calls
    assert wire.admin.roles["usr-2"] == "user"
    assert ("set_user_disabled", "usr-2", "True") in wire.admin.calls
    assert wire.users.rows["usr-2"]["disabled"] is True


async def test_update_member_cannot_disable_last_admin(wire):
    # The platform refuses; the plugin maps it to a conflict and kills no credential.
    _add_user(wire, "usr-1", role="admin")
    wire.sessions.rows["th"] = {"user_id": "usr-1", "last_seen_at": future(0), "absolute_expires_at": future()}
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(disabled=True)
        )
    assert "th" in wire.sessions.rows
    assert wire.users.rows["usr-1"]["disabled"] is False


async def test_update_member_cannot_demote_last_admin(wire):
    _add_user(wire, "usr-1", role="admin")
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(
            UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(role="viewer")
        )
    assert wire.admin.roles["usr-1"] == "admin"


async def test_update_member_any_enabled_admin_principal_counts(wire):
    # An admin principal the platform holds outside this plugin (a keys-only owner) counts:
    # demoting the plugin's only admin user is allowed while it stays enabled.
    _add_user(wire, "usr-1", role="admin")
    wire.admin.roles["keys-only-owner"] = "admin"
    await _provider(wire).invoke_member_action(UPDATE_MEMBER, target="usr-1", payload=UpdateMemberInput(role="viewer"))
    assert wire.admin.roles["usr-1"] == "viewer"


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
    # The platform refuses before anything of the plugin's own is touched.
    _add_user(wire, "usr-1", role="admin", password_hash="h")
    wire.sessions.rows["th"] = {"user_id": "usr-1", "last_seen_at": future(0), "absolute_expires_at": future()}
    with pytest.raises(MemberActionConflictError, match="last enabled admin"):
        await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-1", payload=NoInput())
    assert "usr-1" in wire.users.rows
    assert "th" in wire.sessions.rows


async def test_remove_member_retried_after_a_mid_way_failure_completes(wire, monkeypatch):
    # The principal goes first; when a later step fails, the user row is still there and the
    # removal is re-run: the retry finds the principal already gone and completes the rest.
    _add_user(wire, "usr-1", role="admin")  # keeps an admin alive
    _add_user(wire, "usr-2", role="viewer", password_hash="h")
    wire.sessions.rows["th"] = {"user_id": "usr-2", "last_seen_at": future(0), "absolute_expires_at": future()}
    real_delete = wire.sessions.delete_for_user

    async def failing_delete(user_id, keep_token_hash=None):
        raise RuntimeError("sessions store unavailable")

    monkeypatch.setattr(wire.sessions, "delete_for_user", failing_delete)
    with pytest.raises(RuntimeError, match="sessions store unavailable"):
        await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-2", payload=NoInput())
    assert "usr-2" in wire.users.rows
    assert "usr-2" not in wire.admin.roles

    monkeypatch.setattr(wire.sessions, "delete_for_user", real_delete)
    result = await _provider(wire).invoke_member_action(REMOVE_MEMBER, target="usr-2", payload=NoInput())
    assert isinstance(result, NoResult)
    assert "usr-2" not in wire.users.rows
    assert wire.sessions.rows == {}


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
    assert member.role == "admin"
    assert member.actions == list(MEMBER_ROW_ACTIONS)

    (invite,) = listing.invites
    assert invite.role == "viewer"
    assert invite.actions == list(INVITE_ROW_ACTIONS)
