"""Tests for the accounts-provider registry, the ``AccountsProvider`` ABC, and
the ``AccountsAdminServices`` / ``AccountsProviderSettings`` Protocols.

The accounts registry is handle-free like the identity registry, with two
deliberate additions exercised here: dual registration into the identity
registry (an accounts provider IS its own sessions' token answerer) and a
name-sorted enumeration snapshot.
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.registry import (
    get_identity_provider_factory,
    register_identity_provider,
)
from tai42_contract.access_control.registry import reset_registry as reset_identity_registry
from tai42_contract.accounts.models import FormField, FormMethod, LoginMethod
from tai42_contract.accounts.provider import (
    AccountsAdminServices,
    AccountsProvider,
    AccountsProviderSettings,
)
from tai42_contract.accounts.registry import (
    get_accounts_provider_factory,
    iter_accounts_provider_factories,
    register_accounts_provider,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registries():  # pyright: ignore[reportUnusedFunction]
    # Both registries are module-global state, and register_accounts_provider
    # dual-writes into the identity registry; isolate every test from the others.
    reset_registry()
    reset_identity_registry()
    yield
    reset_registry()
    reset_identity_registry()


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

    async def needs_bootstrap(self) -> bool:
        return False

    async def revoke_session(self, token: str) -> bool:
        return token == "tai-sess-ok"


def _fake_factory(*_args: object, **_kwargs: object) -> AccountsProvider:
    return _FakeAccounts()


# -- Registry ------------------------------------------------------------------


def test_register_then_lookup_returns_the_factory():
    register_accounts_provider("fake", _fake_factory)
    assert get_accounts_provider_factory("fake") is _fake_factory
    assert isinstance(get_accounts_provider_factory("fake")(), _FakeAccounts)


def test_registration_dual_registers_into_identity_registry():
    # An accounts provider is the identity answerer for its own sessions, so a
    # single accounts registration lands the SAME factory in the identity registry.
    register_accounts_provider("fake", _fake_factory)
    assert get_identity_provider_factory("fake") is _fake_factory


def test_identity_collision_raises_and_leaves_accounts_map_untouched():
    # A name already taken in the identity registry must make the accounts
    # registration raise (identity write is ordered first) without registering
    # anything in the accounts map.
    register_identity_provider("taken", _fake_factory)
    with pytest.raises(ValueError, match="already registered"):
        register_accounts_provider("taken", _fake_factory)
    with pytest.raises(KeyError, match="Unknown accounts provider"):
        get_accounts_provider_factory("taken")


def test_iter_is_name_sorted_and_a_fresh_list():
    register_accounts_provider("bravo", _fake_factory)
    register_accounts_provider("alpha", _fake_factory)
    snapshot = iter_accounts_provider_factories()
    assert [name for name, _ in snapshot] == ["alpha", "bravo"]
    # Mutating the returned list must not affect a subsequent call.
    snapshot.clear()
    assert [name for name, _ in iter_accounts_provider_factories()] == ["alpha", "bravo"]


def test_duplicate_name_raises_valueerror():
    register_accounts_provider("fake", _fake_factory)
    with pytest.raises(ValueError, match="'fake' already registered"):
        register_accounts_provider("fake", _fake_factory)


def test_unknown_name_raises_keyerror():
    with pytest.raises(KeyError, match="Unknown accounts provider: 'nope'"):
        get_accounts_provider_factory("nope")


def test_reset_registry_clears_only_the_accounts_map():
    register_accounts_provider("fake", _fake_factory)
    reset_registry()
    with pytest.raises(KeyError):
        get_accounts_provider_factory("fake")
    assert iter_accounts_provider_factories() == []
    # decision 5b: the accounts reset touches ONLY the accounts map — the identity
    # registry is reset separately by the application's start(), so the dual
    # registration survives an accounts-only reset.
    assert get_identity_provider_factory("fake") is _fake_factory


# -- AccountsProvider ABC ------------------------------------------------------


def test_abstract_provider_cannot_instantiate_directly():
    assert AccountsProvider.__abstractmethods__
    with pytest.raises(TypeError):
        AccountsProvider()  # pyright: ignore[reportAbstractUsage]


class _MissingLoginMethods(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    async def needs_bootstrap(self) -> bool:
        return False

    async def revoke_session(self, token: str) -> bool:
        return False


class _MissingNeedsBootstrap(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def revoke_session(self, token: str) -> bool:
        return False


class _MissingRevokeSession(AccountsProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    def login_methods(self) -> list[LoginMethod]:
        return []

    async def needs_bootstrap(self) -> bool:
        return False


class _MissingValidateToken(AccountsProvider):
    def login_methods(self) -> list[LoginMethod]:
        return []

    async def needs_bootstrap(self) -> bool:
        return False

    async def revoke_session(self, token: str) -> bool:
        return False


@pytest.mark.parametrize(
    "cls",
    [
        _MissingLoginMethods,
        _MissingNeedsBootstrap,
        _MissingRevokeSession,
        _MissingValidateToken,
    ],
)
def test_subclass_missing_any_abstract_method_cannot_instantiate(cls: type[AccountsProvider]):
    with pytest.raises(TypeError):
        cls()  # pyright: ignore[reportAbstractUsage]


def test_full_subclass_instantiates_and_inherits_concrete_members():
    async def run() -> None:
        provider = _FakeAccounts()
        # The three abstract members answer.
        methods = provider.login_methods()
        assert len(methods) == 1
        assert await provider.needs_bootstrap() is False
        assert await provider.revoke_session("tai-sess-ok") is True
        assert await provider.revoke_session("foreign") is False
        assert await provider.validate_token("tai-sess-ok") == AuthIdentity(user_id="u1", claims={})
        assert await provider.validate_token("nope") is None
        # Inherited concrete members from IdentityProvider are locked in place.
        assert await provider.healthcheck() is None
        assert provider.readiness_targets() == ()

    asyncio.run(run())


# -- Protocols -----------------------------------------------------------------


class _StandInAdmin:
    async def apply_role(self, user_id: str, role: str) -> None:
        return None

    async def remove_policy(self, user_id: str) -> None:
        return None

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        return None


class _StandInSettings:
    def __init__(self) -> None:
        self.redis = object()
        self.admin = _StandInAdmin()


def test_admin_services_protocol_is_runtime_checkable():
    assert isinstance(_StandInAdmin(), AccountsAdminServices)
    assert not isinstance(object(), AccountsAdminServices)


def test_settings_protocol_is_runtime_checkable():
    assert isinstance(_StandInSettings(), AccountsProviderSettings)

    class _MissingAdmin:
        def __init__(self) -> None:
            self.redis = object()

    class _MissingRedis:
        def __init__(self) -> None:
            self.admin = _StandInAdmin()

    assert not isinstance(_MissingAdmin(), AccountsProviderSettings)
    assert not isinstance(_MissingRedis(), AccountsProviderSettings)
