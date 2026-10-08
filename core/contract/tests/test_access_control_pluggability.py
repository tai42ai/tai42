"""Tests for the ``ApiKeyIdentityProvider`` provisioning ABC and the ``IdentityProviderSettings``
Protocol — the contract surface for pluggable identity providers."""

from __future__ import annotations

import asyncio
from typing import get_protocol_members

import pytest

from tai42_contract.access_control.identity import (
    ApiKeyIdentityProvider,
    AuthIdentity,
    IdentityProvider,
    IdentityProviderSettings,
    ReadinessTarget,
)


class _FakeProvider(IdentityProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return AuthIdentity(user_id="u", claims={}) if token == "good" else None


# -- ApiKeyIdentityProvider ABC ------------------------------------------------


class _FullApiKeyProvider(ApiKeyIdentityProvider):
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return AuthIdentity(user_id="u", claims={}) if token in self._store.values() else None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str) -> str:
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

    async def provision(self, user_id: str, description: str, *, owner_user_id: str) -> str:
        return "k"

    async def revoke(self, user_id: str) -> bool:
        return False


def test_full_subclass_implements_every_method():
    async def run() -> None:
        provider = _FullApiKeyProvider()
        raw = await provider.provision("alice", "laptop", owner_user_id="owner-1")
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


def test_settings_marker_names_no_backing_store_field():
    # The marker Protocol carries no field: a provider reads its OWN configuration
    # (connection handles, key namespaces) from its own settings, so any identity
    # provider's injected settings object satisfies the seam regardless of backing
    # store. The absence of a declared field is what keeps the contract store-agnostic.
    assert get_protocol_members(IdentityProviderSettings) == frozenset()
