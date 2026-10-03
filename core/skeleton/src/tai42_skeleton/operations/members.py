"""The members-listing operation fanning out over the accounts-provider registry.

``list_members`` aggregates every registered accounts provider's people and
outstanding invitations into one deployment-wide view, so a generic Members
screen can render without knowing which providers are installed. Each provider
supplies its own :class:`~tai42_contract.accounts.models.MemberListing` through
the contract seam; the aggregate wraps each entry into a wire row carrying the
opaque routing tokens (handle + applicable action keys) and, for a member, the
access-control ``disabled`` state JOINED from the platform's own principal records.

The operation fans out over the CURRENT epoch's live accounts-provider instances —
the ones the epoch build eagerly instantiated and recorded — rather than
re-instantiating a provider per call, the same source
:mod:`~tai42_skeleton.operations.login` reads. Provider errors propagate (loud,
never a silently dropped provider's accounts), and a principal id a provider names
that the platform store does not hold raises rather than inventing a state.
"""

from __future__ import annotations

from tai42_contract.accounts import (
    InviteRow,
    MemberDirectory,
    MemberPrincipalState,
    MemberRow,
)

from tai42_skeleton.access_control import management
from tai42_skeleton.operations import ForbiddenError, OperationFailedError, operation
from tai42_skeleton.operations._authority import require_admin, resolve_caller
from tai42_skeleton.operations.member_actions import (
    _active_accounts_provider_items,
    _encode_action_key,
    _encode_handle,
)


def _joined_disabled(disabled_by_principal: dict[str, bool], provider_name: str, principal_id: str) -> bool:
    """The platform principal's ``disabled`` state for ``principal_id``, or raise loudly.

    The access-control owner of ``disabled`` is the platform principal record; a provider
    names only the principal ids a person holds. A named id the platform store does not hold
    is a provider defect — refused loudly with the provider and the id, never defaulted.
    """
    if principal_id not in disabled_by_principal:
        raise OperationFailedError(
            f"accounts provider {provider_name!r} named principal id {principal_id!r} "
            "that the platform principal store does not hold"
        )
    return disabled_by_principal[principal_id]


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
    name order, minting each row's opaque handle and action keys and joining each member's
    per-principal ``disabled`` state from the platform's own principal store (read once per
    call). A member row's ``disabled`` is derived True only when EVERY principal is disabled.
    Provider errors propagate (loud, never a silently empty listing).
    """
    caller = await resolve_caller()
    require_admin(caller)
    disabled_by_principal = {row["user_id"]: row["disabled"] for row in await management.list_principals()}
    members: list[MemberRow] = []
    invites: list[InviteRow] = []
    for name, provider in _active_accounts_provider_items():
        listing = await provider.list_members()
        for entry in listing.members:
            principals = [
                MemberPrincipalState(
                    user_id=principal_id,
                    disabled=_joined_disabled(disabled_by_principal, name, principal_id),
                )
                for principal_id in entry.principal_ids
            ]
            members.append(
                MemberRow(
                    id=entry.id,
                    email=entry.email,
                    role=entry.role,
                    created_at=entry.created_at,
                    principals=principals,
                    disabled=all(state.disabled for state in principals),
                    handle=_encode_handle(name, entry.id),
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
                action_keys=[_encode_action_key(name, action_id) for action_id in entry.actions],
            )
            for entry in listing.invites
        )
    return MemberDirectory(members=members, invites=invites)
