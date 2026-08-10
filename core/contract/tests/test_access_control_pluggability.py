"""Tests for the module-level identity-provider registry, the
``ApiKeyIdentityProvider`` provisioning ABC, and the ``IdentityProviderSettings``
Protocol — all new contract surface for pluggable identity providers.

The registry is deliberately app-handle-free: registration works with NO bound
``tai42_app`` (no ``bind()``, no ``start()``), so a plugin registers by direct
module import in any process.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from tai42_contract.access_control.identity import (
    ApiKeyIdentityProvider,
    AuthIdentity,
    IdentityProvider,
    IdentityProviderSettings,
    ReadinessTarget,
)
from tai42_contract.access_control.registry import (
    get_identity_provider_factory,
    register_identity_provider,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():  # pyright: ignore[reportUnusedFunction]
    # The registry is module-global state; isolate every test from the others.
    reset_registry()
    yield
    reset_registry()


class _FakeProvider(IdentityProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return AuthIdentity(user_id="u", claims={}) if token == "good" else None


def _fake_factory(*_args: Any, **_kwargs: Any) -> IdentityProvider:
    return _FakeProvider()


# -- Registry ------------------------------------------------------------------


def test_register_then_lookup_returns_the_factory():
    # No bind(), no tai42_app anywhere — a plain module-level call.
    register_identity_provider("fake", _fake_factory)
    assert get_identity_provider_factory("fake") is _fake_factory
    # The factory builds a live provider.
    assert isinstance(get_identity_provider_factory("fake")(), _FakeProvider)


def test_duplicate_name_raises_valueerror():
    register_identity_provider("fake", _fake_factory)
    with pytest.raises(ValueError, match="already registered"):
        register_identity_provider("fake", _fake_factory)


def test_unknown_name_raises():
    with pytest.raises(KeyError, match="Unknown identity provider"):
        get_identity_provider_factory("nope")


def test_reset_registry_clears():
    register_identity_provider("fake", _fake_factory)
    reset_registry()
    with pytest.raises(KeyError):
        get_identity_provider_factory("fake")
    # After a reset the SAME name re-registers without tripping the dup guard —
    # the reload path the skeleton's start() relies on.
    register_identity_provider("fake", _fake_factory)
    assert get_identity_provider_factory("fake") is _fake_factory


def test_plugin_shape_registers_at_module_import():
    # A plugin module body just calls the directly-imported registration function
    # at import time. Executing such a module body lands the factory in the
    # registry — no app handle, no bind().
    source = (
        "from tai42_contract.access_control.registry import register_identity_provider\n"
        "register_identity_provider('plugin', lambda *a, **k: object())\n"
    )
    module = types.ModuleType("fake_identity_plugin")
    exec(compile(source, "fake_identity_plugin", "exec"), module.__dict__)
    assert get_identity_provider_factory("plugin") is not None


# -- ApiKeyIdentityProvider ABC ------------------------------------------------


class _FullApiKeyProvider(ApiKeyIdentityProvider):
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return AuthIdentity(user_id="u", claims={}) if token in self._store.values() else None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        self._store[user_id] = description
        return f"raw-key-for-{user_id}"

    async def revoke(self, user_id: str) -> bool:
        return self._store.pop(user_id, None) is not None

    async def update_description(self, user_id: str, description: str) -> bool:
        if user_id not in self._store:
            return False
        self._store[user_id] = description
        return True

    async def list_identities(self) -> list[tuple[str, str]]:
        return list(self._store.items())


class _PartialApiKeyProvider(ApiKeyIdentityProvider):
    # Deliberately missing update_description / list_identities.
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        return "k"

    async def revoke(self, user_id: str) -> bool:
        return False


def test_full_subclass_implements_every_method():
    async def run() -> None:
        provider = _FullApiKeyProvider()
        raw = await provider.provision("alice", "laptop")
        assert raw == "raw-key-for-alice"
        assert await provider.list_identities() == [("alice", "laptop")]
        assert await provider.update_description("alice", "phone") is True
        assert await provider.list_identities() == [("alice", "phone")]
        assert await provider.update_description("bob", "x") is False
        assert await provider.revoke("alice") is True
        assert await provider.revoke("alice") is False

    asyncio.run(run())


def test_healthcheck_default_is_a_no_op():
    # healthcheck lives on the BASE IdentityProvider, so a plain (non-minting)
    # provider carries the default no-op too — the skeleton boot-probes any provider.
    assert asyncio.run(_FakeProvider().healthcheck()) is None
    assert asyncio.run(_FullApiKeyProvider().healthcheck()) is None


def test_readiness_targets_default_is_empty():
    # readiness_targets lives on the BASE IdentityProvider, so a provider with no
    # pingable backing store inherits the empty default — core enumerates it and adds
    # no readiness check, exactly as it inherits the healthcheck no-op.
    assert list(_FakeProvider().readiness_targets()) == []
    assert list(_FullApiKeyProvider().readiness_targets()) == []


class _FakeClient:
    """Stands in for a kit client class — the contract types it as a bare ``type``."""


_sentinel_settings = object()  # opaque connection settings; the contract types it Any


def test_readiness_target_declares_name_client_and_settings():
    # A provider backed by its own store overrides the default to declare a target
    # core pings generically — a name, the client CLASS, and its settings.
    class _StoreBackedProvider(IdentityProvider):
        async def validate_token(self, token: str) -> AuthIdentity | None:
            return None

        def readiness_targets(self) -> tuple[ReadinessTarget, ...]:
            return (ReadinessTarget("identity_store", _FakeClient, _sentinel_settings),)

    (target,) = _StoreBackedProvider().readiness_targets()
    assert target.name == "identity_store"
    assert target.client is _FakeClient
    assert target.settings is _sentinel_settings


def test_incomplete_subclass_cannot_instantiate():
    assert ApiKeyIdentityProvider.__abstractmethods__
    with pytest.raises(TypeError):
        _PartialApiKeyProvider()  # pyright: ignore[reportAbstractUsage]


# -- IdentityProviderSettings Protocol -----------------------------------------


class _StandInSettings:
    # A duck-typed stand-in for skeleton's AccessControlSettings — it names no
    # contract class, only the fields a provider factory reads.
    def __init__(self) -> None:
        self.key_prefix = "ac:key:"
        self.redis = object()  # kit connection settings in production; opaque here


class _MissingKeyPrefix:
    def __init__(self) -> None:
        self.redis = object()


def test_stand_in_structurally_satisfies_settings_protocol():
    assert isinstance(_StandInSettings(), IdentityProviderSettings)


def test_object_missing_a_field_is_rejected():
    assert not isinstance(_MissingKeyPrefix(), IdentityProviderSettings)
