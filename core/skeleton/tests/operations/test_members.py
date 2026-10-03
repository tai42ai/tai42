"""The members-listing aggregation operation.

``list_members`` fans out over the CURRENT epoch's recorded accounts providers and
concatenates each provider's :class:`MemberListing` into one deployment-wide view. A
NEUTRAL accounts provider — neither of the shipped implementations — proves another
accounts provider appears in Members as-is: the aggregation reads only the generic
contract seam, so nothing in the platform names or shapes itself around one provider.

The admin fence (``require_admin``) and its loud propagation of a provider error are
covered here too.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.models import AccessPolicy
from tai42_contract.accounts import (
    AccountsProvider,
    InviteEntry,
    LoginMethod,
    MemberEntry,
    MemberListing,
)

from tai42_skeleton.operations import ForbiddenError
from tai42_skeleton.operations import members as members_ops
from tai42_skeleton.operations._authority import Caller


class _NeutralAccounts(AccountsProvider):
    """An accounts provider owned by neither shipped implementation.

    It supplies its people and invitations straight through the contract seam, so the
    aggregation can surface them with no platform change — the proof the Members listing
    is agnostic to each accounts implementation.
    """

    def __init__(self, listing: MemberListing, *, list_raises: bool = False) -> None:
        self._listing = listing
        self._list_raises = list_raises

    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused
        return None

    def login_methods(self) -> list[LoginMethod]:  # pragma: no cover - unused
        return []

    async def list_members(self) -> MemberListing:
        if self._list_raises:
            raise RuntimeError("provider store down")
        return self._listing

    async def revoke_session(self, token: str) -> bool:  # pragma: no cover - unused
        return False


def _member(id_: str) -> MemberEntry:
    return MemberEntry(id=id_, email=f"{id_}@x.test", role="editor", disabled=False, created_at=datetime.now(UTC))


def _invite(id_: str) -> InviteEntry:
    now = datetime.now(UTC)
    return InviteEntry(id=id_, email=f"{id_}@x.test", role="viewer", created_at=now, expires_at=now)


@pytest.fixture(autouse=True)
def _clean_active_providers():
    """Isolate the test on the serving core's recorded accounts providers."""
    from tai42_skeleton.app.instance import app

    core = app._serving_core
    saved = dict(core.active_auth_providers)
    core.active_auth_providers.clear()
    try:
        yield
    finally:
        core.active_auth_providers.clear()
        core.active_auth_providers.update(saved)


@pytest.fixture
def admin_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _admin() -> Caller:
        return Caller(caller_id="admin1", policy=AccessPolicy(scopes=["*"]), is_admin=True, owner_claim=None)

    monkeypatch.setattr(members_ops, "resolve_caller", _admin)


@pytest.fixture
def non_admin_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _editor() -> Caller:
        return Caller(caller_id="editor1", policy=AccessPolicy(scopes=["read"]), is_admin=False, owner_claim=None)

    monkeypatch.setattr(members_ops, "resolve_caller", _editor)


def _register(name: str, provider: AccountsProvider) -> None:
    from tai42_skeleton.app.instance import app

    app._serving_core.active_auth_providers[name] = provider


async def test_empty_registry_lists_nothing(admin_caller: None) -> None:
    listing = await members_ops.list_members()
    assert listing.members == []
    assert listing.invites == []


async def test_neutral_provider_members_and_invites_appear_as_is(admin_caller: None) -> None:
    _register(
        "neutral",
        _NeutralAccounts(MemberListing(members=[_member("usr-1")], invites=[_invite("usr-2")])),
    )
    listing = await members_ops.list_members()
    assert [m.id for m in listing.members] == ["usr-1"]
    assert [i.id for i in listing.invites] == ["usr-2"]


async def test_two_providers_concatenate_in_name_order(admin_caller: None) -> None:
    _register("bravo", _NeutralAccounts(MemberListing(members=[_member("b-mem")], invites=[_invite("b-inv")])))
    _register("alpha", _NeutralAccounts(MemberListing(members=[_member("a-mem")], invites=[_invite("a-inv")])))
    listing = await members_ops.list_members()
    # Name-sorted provider order: alpha before bravo, members and invites kept separate.
    assert [m.id for m in listing.members] == ["a-mem", "b-mem"]
    assert [i.id for i in listing.invites] == ["a-inv", "b-inv"]


async def test_provider_error_propagates(admin_caller: None) -> None:
    _register("neutral", _NeutralAccounts(MemberListing(members=[], invites=[]), list_raises=True))
    with pytest.raises(RuntimeError, match="provider store down"):
        await members_ops.list_members()


async def test_non_admin_is_refused(non_admin_caller: None) -> None:
    _register("neutral", _NeutralAccounts(MemberListing(members=[_member("usr-1")], invites=[])))
    with pytest.raises(ForbiddenError):
        await members_ops.list_members()
