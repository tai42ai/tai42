"""The provider's member-admin actions, declared and invoked through the generic seam.

An accounts provider declares its member-admin actions as data
(:meth:`~tai42_contract.accounts.provider.AccountsProvider.member_actions`) and performs
one on demand (``invoke_member_action``); the platform renders and routes them without
knowing what any of them mean. This module holds this provider's own declarations — invite
a user, send a new login link, cancel an invitation, change a member's role or access, and
remove a member — in its own vocabulary, with the Postgres bodies that back them.

Each action names its own input and result models. A correctable failure is raised as a
member-action error from :mod:`tai42_contract.accounts.errors`, so the generic invoke
operation maps it to the right status without this plugin importing the application:
a taken email or the platform's last-admin refusal is a conflict, an unknown target is
not-found, an unknown role is a bad request. The last-admin rule itself is the platform's:
every role change, disable and removal goes through the injected admin services, which
refuse a change that would leave no enabled admin principal. The one-time invite link a mint produces is
surfaced on the action's result model, the only place it is ever shown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field
from tai42_contract.access_control import get_current_user_id
from tai42_contract.accounts import MemberAction
from tai42_contract.accounts.errors import (
    LastAdminError,
    MemberActionBadRequestError,
    MemberActionConflictError,
    MemberActionNotFoundError,
)
from tai42_contract.template import TemplatedText

from tai42_accounts_postgres import service
from tai42_accounts_postgres.settings import accounts_settings
from tai42_accounts_postgres.stores import EmailTakenError

if TYPE_CHECKING:
    from tai42_contract.accounts import AccountsAdminServices, AccountsProviderSettings

# This provider's own member-action ids. The platform maps each to an opaque catalog key
# and never parses it; they are this plugin's private vocabulary.
INVITE_USER = "invite_user"
RESEND_INVITE = "resend_invite"
CANCEL_INVITE = "cancel_invite"
UPDATE_MEMBER = "update_member"
REMOVE_MEMBER = "remove_member"

# Which of this provider's actions apply to each kind of row, reported per row by
# ``list_members``. A member row offers a role/access change and removal; an invitation
# offers a fresh login link and cancellation.
MEMBER_ROW_ACTIONS = (UPDATE_MEMBER, REMOVE_MEMBER)
INVITE_ROW_ACTIONS = (RESEND_INVITE, CANCEL_INVITE)


class InviteUserInput(BaseModel):
    """The input for inviting a user: the new account's ``email`` and platform ``role``."""

    email: str = Field(min_length=1)
    role: str = Field(min_length=1)


class UpdateMemberInput(BaseModel):
    """The input for changing a member: an optional new ``role`` and/or ``disabled`` state."""

    role: str | None = None
    disabled: bool | None = None


class InviteLinkResult(BaseModel):
    """A minted invite: the raw one-time token and its origin-relative login path, shown once."""

    invite_token: str
    login_path: str


class NoInput(BaseModel):
    """An action that takes no input beyond the row it targets."""


class NoResult(BaseModel):
    """An action that produces nothing to show."""


def declare_member_actions() -> list[MemberAction]:
    """This provider's member-admin actions, in its own vocabulary.

    Static, config-derived metadata. ``invite_user`` is a page-level action (it creates a
    new account); the others act on a row. A destructive action (cancel an invitation,
    remove a member) asks the caller to confirm. The labels ride as inline templated text,
    rendered by the platform's resource manager at the catalog door.
    """
    return [
        MemberAction(
            id=INVITE_USER,
            label=TemplatedText(content="Invite a user"),
            scope="page",
            destructive=False,
            input_model=InviteUserInput,
            result_model=InviteLinkResult,
        ),
        MemberAction(
            id=RESEND_INVITE,
            label=TemplatedText(content="Send a new login link"),
            scope="invite_row",
            destructive=False,
            input_model=NoInput,
            result_model=InviteLinkResult,
        ),
        MemberAction(
            id=CANCEL_INVITE,
            label=TemplatedText(content="Cancel invitation"),
            scope="invite_row",
            destructive=True,
            input_model=NoInput,
            result_model=NoResult,
        ),
        MemberAction(
            id=UPDATE_MEMBER,
            label=TemplatedText(content="Change role or access"),
            scope="member_row",
            destructive=False,
            input_model=UpdateMemberInput,
            result_model=NoResult,
        ),
        MemberAction(
            id=REMOVE_MEMBER,
            label=TemplatedText(content="Remove user"),
            scope="member_row",
            destructive=True,
            input_model=NoInput,
            result_model=NoResult,
        ),
    ]


async def invoke(
    settings: AccountsProviderSettings, action_id: str, *, target: str | None, payload: BaseModel
) -> BaseModel:
    """Perform this provider's action named ``action_id`` against ``target``.

    ``payload`` is the already-validated instance of the action's declared input model. A
    correctable failure raises a member-action error the invoke operation maps to a status.
    """
    if action_id == INVITE_USER:
        return await _invite_user(settings, cast("InviteUserInput", payload))
    if action_id == RESEND_INVITE:
        return await _resend_invite(target)
    if action_id == CANCEL_INVITE:
        return await _remove_user(settings, target)
    if action_id == UPDATE_MEMBER:
        return await _update_member(settings, target, cast("UpdateMemberInput", payload))
    if action_id == REMOVE_MEMBER:
        return await _remove_user(settings, target)
    raise MemberActionNotFoundError(f"unknown member action {action_id!r}")


async def _invite_user(settings: AccountsProviderSettings, payload: InviteUserInput) -> InviteLinkResult:
    """Create a human principal with a NULL-password login and a one-time invite.

    Create the principal (its row and role) through the injected admin services, create its
    password-less login row, then mint the invite. A failure after the principal exists
    compensates — any invite and the login row are dropped and the principal removed — so an
    invite stays re-runnable and never leaves a policy-less or login-less account.
    """
    email = service.normalize_email(payload.email)
    admin = settings.admin
    user_id = service.new_user_id()

    try:
        await admin.create_principal(
            user_id,
            kind="human",
            display_name=email,
            created_by=get_current_user_id(),
            role=payload.role,
        )
    except KeyError as exc:
        # Unknown role name; create_principal wrote nothing.
        raise MemberActionBadRequestError(f"unknown role: {payload.role!r}") from exc

    async def _cleanup() -> None:
        await service.invites_store().delete_for_user(user_id)
        await service.users_store().delete(user_id)
        # remove_policy deletes the principal row and its policy (and revokes owned keys).
        await admin.remove_policy(user_id)

    try:
        await service.users_store().create_login(user_id, email, password_hash=None)
    except EmailTakenError as exc:
        await _cleanup()
        raise MemberActionConflictError("email already registered") from exc

    try:
        raw_invite = service.new_invite_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=accounts_settings().invite_ttl_seconds)
        await service.invites_store().create(service.token_hash(raw_invite), user_id, expires_at)
    except Exception:
        await _cleanup()
        raise

    return InviteLinkResult(invite_token=raw_invite, login_path=service.invite_login_path(raw_invite))


async def _resend_invite(target: str | None) -> InviteLinkResult:
    """Replace the live invite for a member who has not set a password yet.

    A conflict once a password is set — a set password rotates through the self-service
    password route, not an admin invite.
    """
    if target is None:
        raise MemberActionBadRequestError("sending a login link requires a target invitation")
    existing = await service.users_store().get_by_user_id(target)
    if existing is None:
        raise MemberActionNotFoundError("user not found")
    if existing["password_hash"] is not None:
        raise MemberActionConflictError("user already has a password")

    raw_invite = service.new_invite_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=accounts_settings().invite_ttl_seconds)
    await service.invites_store().create(service.token_hash(raw_invite), target, expires_at)
    return InviteLinkResult(invite_token=raw_invite, login_path=service.invite_login_path(raw_invite))


async def _update_member(
    settings: AccountsProviderSettings, target: str | None, payload: UpdateMemberInput
) -> NoResult:
    """Change a member's role and/or disabled state.

    The current role is the one the member's principal holds. A role change and a disable
    go through the admin services, which refuse the last enabled admin (a conflict). A
    disable kills credentials first: the principal is disabled, then the sessions are
    deleted, then the plugin's own row is marked; a re-enable reverses it (marker last).
    """
    if target is None:
        raise MemberActionBadRequestError("changing a member requires a target member")
    store = service.users_store()
    existing = await store.get_by_user_id(target)
    if existing is None:
        raise MemberActionNotFoundError("user not found")
    admin = settings.admin

    if payload.role is not None and payload.role != await _current_role(admin, target):
        try:
            await admin.apply_role(target, payload.role)
        except LastAdminError as exc:
            raise MemberActionConflictError(str(exc)) from exc
        except KeyError as exc:
            # Unknown role name; ``apply_role`` wrote nothing.
            raise MemberActionBadRequestError(f"unknown role: {payload.role!r}") from exc

    if payload.disabled is not None and payload.disabled != existing["disabled"]:
        if payload.disabled:
            try:
                await admin.set_user_disabled(target, True)
            except LastAdminError as exc:
                raise MemberActionConflictError(str(exc)) from exc
            await service.sessions_store().delete_for_user(target)
            await store.set_disabled(target, True)
        else:
            await store.set_disabled(target, False)
            await admin.set_user_disabled(target, False)

    return NoResult()


async def _current_role(admin: AccountsAdminServices, user_id: str) -> str | None:
    """The role ``user_id``'s principal holds, or raise when the user has no principal."""
    roles = await admin.principal_roles([user_id])
    if user_id not in roles:
        raise RuntimeError(f"accounts user {user_id!r} has no access-control principal")
    return roles[user_id]


async def _remove_user(settings: AccountsProviderSettings, target: str | None) -> NoResult:
    """Delete a member or cancel an invitation.

    The principal's policy goes first through the admin services (revoking owned keys),
    which refuse the last enabled admin (a conflict, nothing removed); then the sessions,
    the invites and the row. A mid-way failure leaves a re-deletable user, never an
    orphaned live credential: a re-run finds the principal already removed and completes
    the remaining steps.
    """
    if target is None:
        raise MemberActionBadRequestError("removing a member requires a target member")
    store = service.users_store()
    existing = await store.get_by_user_id(target)
    if existing is None:
        raise MemberActionNotFoundError("user not found")
    admin = settings.admin
    # The principal is absent only when an earlier run of this removal took it and then
    # failed on a later step; the re-run goes on with the steps that did not complete.
    if target in await admin.principal_roles([target]):
        try:
            await admin.remove_policy(target)
        except LastAdminError as exc:
            raise MemberActionConflictError(str(exc)) from exc
    await service.sessions_store().delete_for_user(target)
    await service.invites_store().delete_for_user(target)
    await store.delete(target)
    return NoResult()
