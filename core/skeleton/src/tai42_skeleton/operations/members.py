"""The members-listing operation fanning out over the accounts-provider registry.

``list_members`` aggregates every registered accounts provider's people and
outstanding invitations into one deployment-wide view, so a generic Members
screen can render without knowing which providers are installed. Each provider
supplies its own :class:`~tai42_contract.accounts.models.MemberListing` through
the contract seam; the aggregate concatenates them, preserving each provider's
partition of accounts between ``members`` and ``invites``.

The operation fans out over the CURRENT epoch's live accounts-provider instances —
the ones the epoch build eagerly instantiated and recorded — rather than
re-instantiating a provider per call, the same source
:mod:`~tai42_skeleton.operations.login` reads. Provider errors propagate (loud,
never a silently dropped provider's accounts).
"""

from __future__ import annotations

from tai42_contract.accounts import AccountsProvider, InviteEntry, MemberEntry, MemberListing

from tai42_skeleton.operations import ForbiddenError, operation
from tai42_skeleton.operations._authority import require_admin, resolve_caller


def _active_accounts_providers() -> list[AccountsProvider]:
    """The CURRENT epoch's live accounts-provider instances, name-sorted for a deterministic aggregate.

    Filters the epoch's recorded identity providers to the accounts ones (an accounts
    provider IS an identity provider); reads through the serving core so a build in flight
    resolves the epoch being built, else the live one.
    """
    from tai42_skeleton.app.instance import app

    recorded = app._serving_core.active_auth_providers
    return [p for _name, p in sorted(recorded.items()) if isinstance(p, AccountsProvider)]


@operation(
    summary="List members and invites",
    tags=["access-control"],
    errors=[ForbiddenError],
    response_model=MemberListing,
)
async def list_members() -> MemberListing:
    """Aggregate every registered accounts provider's people and invitations (admin only).

    Each provider's :meth:`~tai42_contract.accounts.provider.AccountsProvider.list_members`
    supplies its own members and invites; the aggregate concatenates them in provider
    name order. Provider errors propagate (loud, never a silently empty listing).
    """
    caller = await resolve_caller()
    require_admin(caller)
    members: list[MemberEntry] = []
    invites: list[InviteEntry] = []
    for provider in _active_accounts_providers():
        listing = await provider.list_members()
        members.extend(listing.members)
        invites.extend(listing.invites)
    return MemberListing(members=members, invites=invites)
