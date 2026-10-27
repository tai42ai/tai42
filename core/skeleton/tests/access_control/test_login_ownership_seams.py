"""The two provider abilities the principal and setup doors read, proven with neutral providers.

A provider that only keeps login records (``LoginOwningProvider``) is asked before a principal
DELETE but never before a disable, never handed a setup credential, and never advertised as a
setup login. A provider that holds a credential (``LoginAttachingProvider``) is asked before
both and is the one the setup door hands the owner's credential to.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import BaseModel
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider
from tai42_contract.accounts import LoginAttachingProvider, LoginOwningProvider
from tai42_contract.accounts.models import (
    LoginAttachment,
    LoginCredential,
    MemberAction,
    MemberListing,
    SessionRevocation,
)

from tai42_skeleton.operations import principals as principals_ops
from tai42_skeleton.operations import setup as setup_ops


class _RecordsOnly(LoginOwningProvider):
    """Keeps login records for principals; holds no credential it could attach."""

    def login_methods(self) -> list:  # pragma: no cover - unused
        return []

    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused
        return None

    async def revoke_session(self, token: str) -> SessionRevocation | None:  # pragma: no cover - unused
        return None

    async def list_members(self) -> MemberListing:  # pragma: no cover - unused
        return MemberListing(members=[], invites=[])

    def display_name(self) -> str:
        return "Alpha"

    def member_actions(self) -> list[MemberAction]:  # pragma: no cover - unused
        return []

    async def invoke_member_action(  # pragma: no cover - unused
        self, action_id: str, *, target: str | None, payload: BaseModel
    ) -> BaseModel:
        raise ValueError(action_id)

    async def has_login(self, user_id: str) -> bool:  # pragma: no cover - unused
        return False


class _WithCredential(_RecordsOnly, LoginAttachingProvider):
    """Holds a credential it can attach."""

    async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:  # pragma: no cover
        return LoginAttachment(attached=True)


@pytest.fixture
def recorded() -> Iterator[dict[str, IdentityProvider]]:
    """The serving core's recorded auth providers, emptied for the test and restored after."""
    from tai42_skeleton.app.instance import app

    active = app._serving_core.active_auth_providers
    saved = dict(active)
    active.clear()
    try:
        yield active
    finally:
        active.clear()
        active.update(saved)


def test_a_records_only_provider_fences_delete_but_not_disable(recorded: dict[str, IdentityProvider]) -> None:
    records = _RecordsOnly()
    recorded["records"] = records
    assert principals_ops._login_owning_providers() == [("records", records)]
    assert principals_ops._login_attaching_providers() == []


def test_a_credential_provider_fences_both(recorded: dict[str, IdentityProvider]) -> None:
    records, credential = _RecordsOnly(), _WithCredential()
    recorded["b-records"] = records
    recorded["a-credential"] = credential
    assert principals_ops._login_owning_providers() == [("a-credential", credential), ("b-records", records)]
    assert principals_ops._login_attaching_providers() == [("a-credential", credential)]


def test_the_setup_door_hands_no_credential_to_a_records_only_provider(recorded: dict[str, IdentityProvider]) -> None:
    # A records-only provider sorting first is not the setup door's attach target.
    recorded["a-records"] = _RecordsOnly()
    assert setup_ops._active_login_attaching_provider() is None
    credential = _WithCredential()
    recorded["b-credential"] = credential
    assert setup_ops._active_login_attaching_provider() is credential
