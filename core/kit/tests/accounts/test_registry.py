"""The kit accounts-provider registry: dual registration into the identity registry and a name-sorted
enumeration snapshot."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.accounts.models import FormField, FormMethod, LoginMethod, MemberAction, MemberListing
from tai42_contract.accounts.provider import AccountsProvider

from tai42_kit.access_control.registry import (
    get_identity_provider_factory,
    register_identity_provider,
)
from tai42_kit.access_control.registry import reset_registry as reset_identity_registry
from tai42_kit.accounts.registry import (
    abort_staging,
    begin_staging,
    commit_staging,
    get_accounts_provider_factory,
    iter_accounts_provider_factories,
    iter_accounts_provider_factories_staged,
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

    async def list_members(self) -> MemberListing:
        return MemberListing(members=[], invites=[])

    def member_actions(self) -> list[MemberAction]:
        return []

    async def invoke_member_action(self, action_id: str, *, target: str | None, payload: BaseModel) -> BaseModel:
        raise ValueError(f"no member action {action_id!r}")

    async def revoke_session(self, token: str) -> bool:
        return token == "tai-sess-ok"


def _fake_factory(*_args: object, **_kwargs: object) -> AccountsProvider:
    return _FakeAccounts()


class _OtherAccounts(_FakeAccounts):
    """A distinct provider class — a real conflict when it claims a taken name."""


def _other_factory(*_args: object, **_kwargs: object) -> AccountsProvider:
    return _OtherAccounts()


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
    # A name already taken in the identity registry by a DIFFERENT provider must make
    # the accounts registration raise (identity write is ordered first) without
    # registering anything in the accounts map. A different factory is a real conflict;
    # the identical factory is reload-safe and covered separately below.
    register_identity_provider("taken", _fake_factory)
    with pytest.raises(ValueError, match="already registered"):
        register_accounts_provider("taken", _other_factory)
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


def test_reregistering_the_same_factory_is_a_reload_safe_no_op():
    # Reload-safety: the hot-reload primitive pops the plugin's modules and
    # re-executes their bodies, re-running the module-level register_accounts_provider.
    # Before the fix the second call raised "already registered" and crashed boot;
    # now it is a quiet no-op in BOTH registries, which stay consistent.
    register_accounts_provider("fake", _fake_factory)
    register_accounts_provider("fake", _fake_factory)  # no raise
    assert get_accounts_provider_factory("fake") is _fake_factory
    assert get_identity_provider_factory("fake") is _fake_factory


def test_reregistering_a_reloaded_factory_object_is_a_no_op():
    # The reload primitive mints a FRESH class object each pass, so a reloaded factory
    # is a different object sharing __module__/__qualname__ — still the same provider.
    register_accounts_provider("dup", _FakeAccounts)
    clone = type("_FakeAccounts", (AccountsProvider,), dict(_FakeAccounts.__dict__))
    clone.__module__ = _FakeAccounts.__module__
    clone.__qualname__ = _FakeAccounts.__qualname__
    assert clone is not _FakeAccounts
    register_accounts_provider("dup", clone)  # no raise: same qualified identity
    assert get_accounts_provider_factory("dup") is _FakeAccounts
    assert get_identity_provider_factory("dup") is _FakeAccounts


def test_different_factory_under_existing_name_still_raises():
    # The real-conflict guard is preserved in BOTH registries: a genuinely different
    # provider claiming a taken name is a loud error, not a silent overwrite.
    register_accounts_provider("fake", _fake_factory)
    with pytest.raises(ValueError, match="'fake' already registered"):
        register_accounts_provider("fake", _other_factory)


def test_unknown_name_raises_keyerror():
    with pytest.raises(KeyError, match="Unknown accounts provider: 'nope'"):
        get_accounts_provider_factory("nope")


def test_reset_registry_clears_only_the_accounts_map():
    register_accounts_provider("fake", _fake_factory)
    reset_registry()
    with pytest.raises(KeyError):
        get_accounts_provider_factory("fake")
    assert iter_accounts_provider_factories() == []
    # The accounts reset touches ONLY the accounts map — the identity
    # registry is reset separately by the application's start(), so the dual
    # registration survives an accounts-only reset.
    assert get_identity_provider_factory("fake") is _fake_factory


def test_staged_generation_isolates_the_committed_one():
    register_accounts_provider("live", _fake_factory)
    begin_staging()
    try:
        assert iter_accounts_provider_factories_staged() == []
        register_accounts_provider("next", _other_factory)
        assert iter_accounts_provider_factories_staged() == [("next", _other_factory)]
        assert iter_accounts_provider_factories() == [("live", _fake_factory)]
        commit_staging()
    finally:
        abort_staging()
    assert iter_accounts_provider_factories() == [("next", _other_factory)]


def test_abort_drops_the_staged_generation():
    register_accounts_provider("live", _fake_factory)
    begin_staging()
    register_accounts_provider("dropped", _other_factory)
    abort_staging()
    assert iter_accounts_provider_factories_staged() == [("live", _fake_factory)]
