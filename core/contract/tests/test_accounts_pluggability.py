"""Tests for the ``AccountsProvider`` ABC and the ``AccountsAdminServices`` /
``AccountsProviderSettings`` Protocols."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.accounts.errors import (
    MemberActionBadRequestError,
    MemberActionConflictError,
    MemberActionError,
    MemberActionNotFoundError,
)
from tai42_contract.accounts.models import (
    FormField,
    FormMethod,
    InviteCredential,
    LoginAttachment,
    LoginCredential,
    LoginMethod,
    MemberAction,
    MemberListing,
    PasswordCredential,
)
from tai42_contract.accounts.provider import (
    AccountsAdminServices,
    AccountsProvider,
    AccountsProviderSettings,
    LoginAttachingProvider,
)
from tai42_contract.errors import ErrorKind, error_kind


class _FakeAccounts(AccountsProvider):
    def __init__(self, settings: object | None = None) -> None:
        self._settings = settings

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return AuthIdentity(user_id="u1", claims={}) if token == "tai-sess-ok" else None

    def login_methods(self) -> list[LoginMethod]:
        return [
            FormMethod(
                id="password",
                title="Sign in",
                fields=[FormField(name="email", label="Email")],
                submit_path="/api/login/password",
            )
        ]

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])

    def member_actions(self) -> list[MemberAction]:
        return []

    async def invoke_member_action(self, action_id: str, *, target: str | None, payload: BaseModel) -> BaseModel:
        raise ValueError(f"no member action {action_id!r}")

    async def revoke_session(self, token: str) -> bool:
        return token == "tai-sess-ok"


# -- AccountsProvider ABC ------------------------------------------------------


def test_abstract_provider_cannot_instantiate_directly():
    assert AccountsProvider.__abstractmethods__
    with pytest.raises(TypeError):
        AccountsProvider()  # pyright: ignore[reportAbstractUsage]


class _MissingLoginMethods(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])

    async def revoke_session(self, token: str) -> bool:
        return False


class _MissingRevokeSession(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])


class _MissingValidateToken(AccountsProvider):
    def login_methods(self) -> list[LoginMethod]:
        return []

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])

    async def revoke_session(self, token: str) -> bool:
        return False


class _MissingListMembers(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def revoke_session(self, token: str) -> bool:
        return False

    def member_actions(self) -> list[MemberAction]:
        return []

    async def invoke_member_action(self, action_id: str, *, target: str | None, payload: BaseModel) -> BaseModel:
        raise ValueError(action_id)


class _MissingMemberActions(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])

    async def revoke_session(self, token: str) -> bool:
        return False

    async def invoke_member_action(self, action_id: str, *, target: str | None, payload: BaseModel) -> BaseModel:
        raise ValueError(action_id)


class _MissingInvokeMemberAction(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])

    async def revoke_session(self, token: str) -> bool:
        return False

    def member_actions(self) -> list[MemberAction]:
        return []


@pytest.mark.parametrize(
    "cls",
    [
        _MissingLoginMethods,
        _MissingRevokeSession,
        _MissingValidateToken,
        _MissingListMembers,
        _MissingMemberActions,
        _MissingInvokeMemberAction,
    ],
)
def test_subclass_missing_any_abstract_method_cannot_instantiate(cls: type[AccountsProvider]):
    with pytest.raises(TypeError):
        cls()  # pyright: ignore[reportAbstractUsage]


def test_full_subclass_instantiates_and_inherits_concrete_members():
    async def run() -> None:
        provider = _FakeAccounts()
        # The abstract members answer.
        methods = provider.login_methods()
        assert len(methods) == 1
        assert await provider.revoke_session("tai-sess-ok") is True
        assert await provider.revoke_session("foreign") is False
        assert await provider.validate_token("tai-sess-ok") == AuthIdentity(user_id="u1", claims={})
        assert await provider.validate_token("nope") is None
        # Inherited concrete members from IdentityProvider are locked in place.
        assert await provider.healthcheck() is None
        assert provider.readiness_targets() == ()

    asyncio.run(run())


# -- LoginAttachingProvider ABC ------------------------------------------------


class _FakeLoginAttaching(_FakeAccounts, LoginAttachingProvider):
    def __init__(self, settings: object | None = None) -> None:
        super().__init__(settings)
        self.logins: set[str] = set()

    async def has_login(self, user_id: str) -> bool:
        return user_id in self.logins

    async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:
        self.logins.add(user_id)
        if credential.kind == "invite":
            return LoginAttachment(attached=True, invite_token="inv-1", login_path="/api/login/accept")
        return LoginAttachment(attached=True)


def test_login_attaching_provider_is_an_accounts_provider():
    # The mix-in is an AccountsProvider, so it flows through the same registry and
    # enforcement seam; a plain accounts provider is NOT a LoginAttachingProvider.
    provider = _FakeLoginAttaching()
    assert isinstance(provider, AccountsProvider)
    assert isinstance(provider, LoginAttachingProvider)
    assert not isinstance(_FakeAccounts(), LoginAttachingProvider)


def test_attach_login_is_abstract_until_implemented():
    class _NoAttach(_FakeAccounts, LoginAttachingProvider):
        async def has_login(self, user_id: str) -> bool:
            return False

    assert "attach_login" in LoginAttachingProvider.__abstractmethods__
    with pytest.raises(TypeError):
        _NoAttach()  # pyright: ignore[reportAbstractUsage]


def test_has_login_is_abstract_until_implemented():
    class _NoHasLogin(_FakeAccounts, LoginAttachingProvider):
        async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:
            return LoginAttachment(attached=True)

    assert "has_login" in LoginAttachingProvider.__abstractmethods__
    with pytest.raises(TypeError):
        _NoHasLogin()  # pyright: ignore[reportAbstractUsage]


def test_has_login_answers_whether_the_provider_holds_a_login():
    async def run() -> None:
        provider = _FakeLoginAttaching()
        assert await provider.has_login("owner-1") is False
        await provider.attach_login("owner-1", credential=PasswordCredential(email="a@x.test", password="pw"))
        assert await provider.has_login("owner-1") is True
        assert await provider.has_login("someone-else") is False

    asyncio.run(run())


def test_attach_login_returns_the_attachment():
    async def run() -> None:
        provider = _FakeLoginAttaching()
        password = await provider.attach_login(
            "owner-1", credential=PasswordCredential(email="a@x.test", password="pw")
        )
        assert password == LoginAttachment(attached=True)
        invite = await provider.attach_login("owner-1", credential=InviteCredential(email="a@x.test"))
        assert invite.invite_token == "inv-1"
        assert invite.login_path == "/api/login/accept"

    asyncio.run(run())


# -- Protocols -----------------------------------------------------------------


class _StandInAdmin:
    async def create_principal(
        self,
        user_id: str,
        *,
        kind: str,
        display_name: str,
        created_by: str | None,
        role: str,
    ) -> None:
        return None

    async def apply_role(self, user_id: str, role: str) -> None:
        return None

    async def remove_policy(self, user_id: str) -> None:
        return None

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        return None


class _StandInSettings:
    def __init__(self) -> None:
        self.admin = _StandInAdmin()


def test_admin_services_protocol_is_runtime_checkable():
    assert isinstance(_StandInAdmin(), AccountsAdminServices)
    assert not isinstance(object(), AccountsAdminServices)


def test_settings_protocol_is_runtime_checkable():
    # ``admin`` (the injected policy-services implementation) is the only field the
    # contract names: a provider reads its own backing-store handles from its own
    # settings, so a stand-in carrying only ``admin`` satisfies the seam.
    assert isinstance(_StandInSettings(), AccountsProviderSettings)

    class _MissingAdmin:
        def __init__(self) -> None:
            self.something_else = object()

    assert not isinstance(_MissingAdmin(), AccountsProviderSettings)


# -- member-action error family ------------------------------------------------


def test_member_action_errors_are_one_family_classified_by_kind():
    # A contract-only provider raises these (it cannot import the application errors); each
    # resolves to the stable kind the invoke operation maps to a status, and all share one
    # base so a single catch reaches every correctable failure.
    assert issubclass(MemberActionNotFoundError, MemberActionError)
    assert issubclass(MemberActionConflictError, MemberActionError)
    assert issubclass(MemberActionBadRequestError, MemberActionError)
    assert error_kind(MemberActionError("rejected")) is ErrorKind.BAD_INPUT
    assert error_kind(MemberActionNotFoundError("missing")) is ErrorKind.NOT_FOUND
    assert error_kind(MemberActionConflictError("taken")) is ErrorKind.CONFLICT
    assert error_kind(MemberActionBadRequestError("malformed")) is ErrorKind.BAD_INPUT
