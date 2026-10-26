"""The members-listing operation fanning out over the accounts-provider registry.

``list_members`` aggregates every registered accounts provider's people and
outstanding invitations into one deployment-wide view, so a generic Members
screen can render without knowing which providers are installed. Each provider
supplies its own :class:`~tai42_contract.accounts.models.MemberListing` through
the contract seam; the aggregate wraps each entry into a wire row carrying the
owning provider's declared display name, the opaque routing tokens (handle +
applicable action keys) and, for a member, each principal's access-control
``disabled`` state and the role it holds, JOINED from the platform's own records.

The operation fans out over the CURRENT epoch's live accounts-provider instances —
the ones the epoch build eagerly instantiated and recorded — rather than
re-instantiating a provider per call, the same source
:mod:`~tai42_skeleton.operations.login` reads. Provider errors propagate (loud,
never a silently dropped provider's accounts), and a principal id a provider names
that the platform store does not hold raises rather than inventing a state.
"""

from __future__ import annotations

from collections.abc import Mapping

from tai42_contract.accounts import (
    InviteRow,
    MemberDirectory,
    MemberPrincipalState,
    MemberRow,
)

from tai42_skeleton.access_control import management, roles
from tai42_skeleton.operations import ForbiddenError, OperationFailedError, operation
from tai42_skeleton.operations._authority import require_admin, resolve_caller
from tai42_skeleton.operations.member_actions import (
    _active_accounts_provider_items,
    _encode_action_key,
    _encode_handle,
)


def _joined_principal(
    disabled_by_principal: dict[str, bool],
    role_by_principal: Mapping[str, str | None],
    provider_name: str,
    principal_id: str,
) -> MemberPrincipalState:
    """The platform principal's joined state for ``principal_id``, or raise loudly.

    The access-control owner of ``disabled`` and of the role is the platform principal
    record; a provider names only the principal ids a person holds. A named id the platform
    store does not hold is a provider defect — refused loudly with the provider and the id,
    never defaulted.
    """
    if principal_id not in disabled_by_principal or principal_id not in role_by_principal:
        raise OperationFailedError(
            f"accounts provider {provider_name!r} named principal id {principal_id!r} "
            "that the platform principal store does not hold"
        )
    return MemberPrincipalState(
        user_id=principal_id,
        disabled=disabled_by_principal[principal_id],
        role=role_by_principal[principal_id],
    )


def _row_role(principals: list[MemberPrincipalState]) -> str | None:
    """The one role every principal holds, or ``None`` when there is no single name."""
    held = {state.role for state in principals}
    return held.pop() if len(held) == 1 else None


@operation(
    summary="List members and invites",
    tags=["access-control"],
    errors=[ForbiddenError],
    response_model=MemberDirectory,
)
async def list_members() -> MemberDirectory:
    """Aggregate every registered accounts provider's people and invitations (admin only).

    Each provider's :meth:`~tai42_contract.accounts.provider.AccountsProvider.list_members`
    supplies its own members and invites; the aggregate wraps them into wire rows in provider
    name order, minting each row's opaque handle and action keys and joining each member
    principal's ``disabled`` state (the principal store, read once per call) and the role it
    holds (one ``principal_roles`` read for every member principal). The provider's declared
    :meth:`~tai42_contract.accounts.provider.AccountsProvider.display_name` rides on every
    member and invite row it owns. A member row's ``disabled`` is derived True only when
    EVERY principal is disabled; its ``role`` is the one role every principal holds, else
    ``None``. Provider errors propagate (loud, never a silently empty listing).
    """
    caller = await resolve_caller()
    require_admin(caller)
    disabled_by_principal = {row["user_id"]: row["disabled"] for row in await management.list_principals()}
    listings = [
        (name, provider.display_name(), await provider.list_members())
        for name, provider in _active_accounts_provider_items()
    ]
    role_by_principal = await roles.principal_roles(
        [
            principal_id
            for _, _, listing in listings
            for entry in listing.members
            for principal_id in entry.principal_ids
        ]
    )
    members: list[MemberRow] = []
    invites: list[InviteRow] = []
    for name, provider_name, listing in listings:
        for entry in listing.members:
            principals = [
                _joined_principal(disabled_by_principal, role_by_principal, name, principal_id)
                for principal_id in entry.principal_ids
            ]
            members.append(
                MemberRow(
                    id=entry.id,
                    email=entry.email,
                    role=_row_role(principals),
                    created_at=entry.created_at,
                    principals=principals,
                    disabled=all(state.disabled for state in principals),
                    handle=_encode_handle(name, entry.id),
                    provider_name=provider_name,
                    action_keys=[_encode_action_key(name, action_id) for action_id in entry.actions],
                )
            )
        invites.extend(
            InviteRow(
                id=entry.id,
                email=entry.email,
                role=entry.role,
                created_at=entry.created_at,
                expires_at=entry.expires_at,
                handle=_encode_handle(name, entry.id),
                provider_name=provider_name,
                action_keys=[_encode_action_key(name, action_id) for action_id in entry.actions],
            )
            for entry in listing.invites
        )
    return MemberDirectory(members=members, invites=invites)
