"""Behavior of ``RedisApiKeyProvider`` against the faked redis seam.

Token validation (RAISES on backend error, ``None`` on unknown token,
``AuthIdentity`` on a stored identity) plus the provisioning surface (provision
writes the identity record hash + reverse lookup and returns a raw key; revoke
deletes both; update_description rewrites the stored description; list_identities
enumerates; healthcheck raises loudly when the record store is broken).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from redis.exceptions import ResponseError
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.access_control.registry import get_identity_provider_factory
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_identity_redis import redis_api_key_provider as provider_module
from tai42_identity_redis.redis_api_key_provider import (
    DuplicateIdentityError,
    RedisApiKeyProvider,
)

from .conftest import FakeRedis, make_client_ctx

_KEY_PREFIX = "ac:key:"
_REVERSE_PREFIX = "ac:management:key:"


@dataclass
class _Settings:
    """A minimal settings shape structurally satisfying ``IdentityProviderSettings``."""

    key_prefix: str = _KEY_PREFIX
    redis: Any = None


def _provider(fake: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> RedisApiKeyProvider:
    monkeypatch.setattr(provider_module, "client_ctx", make_client_ctx(fake))
    return RedisApiKeyProvider(_Settings())


# -- registration hook -------------------------------------------------------


def test_module_registers_redis_provider():
    """Importing the plugin registers ``RedisApiKeyProvider`` as ``"redis"`` in
    the contract's module-level registry, with no app handle involved."""
    assert get_identity_provider_factory("redis") is RedisApiKeyProvider


# -- validate_token ----------------------------------------------------------


async def test_validate_token_returns_identity_on_hit(monkeypatch):
    key = f"{_KEY_PREFIX}{hash_api_key('sk-token')}"
    fake = FakeRedis(hashes={key: {"user_id": "u9", "description": "d", "email": "x@y"}})
    identity = await _provider(fake, monkeypatch).validate_token("sk-token")
    assert isinstance(identity, AuthIdentity)
    assert identity.user_id == "u9"
    assert identity.claims == {"user_id": "u9", "description": "d", "email": "x@y"}


async def test_validate_token_miss_returns_none(monkeypatch):
    assert await _provider(FakeRedis(), monkeypatch).validate_token("sk-token") is None


async def test_validate_token_without_user_id_returns_none(monkeypatch):
    key = f"{_KEY_PREFIX}{hash_api_key('sk-token')}"
    fake = FakeRedis(hashes={key: {"description": "no-user-id"}})
    assert await _provider(fake, monkeypatch).validate_token("sk-token") is None


async def test_validate_token_raises_on_backend_error_fail_closed(monkeypatch):
    """A token-validation backend error fails closed by RAISING (logged at
    source), never by returning ``None`` (a simply-invalid credential)."""
    fake = FakeRedis(raise_hash=RuntimeError("redis down"))
    with pytest.raises(RuntimeError, match="redis down"):
        await _provider(fake, monkeypatch).validate_token("sk-token")


# -- provision ---------------------------------------------------------------


async def test_provision_writes_record_and_reverse_lookup_and_returns_raw_key(monkeypatch):
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)

    raw_key = await provider.provision("u1", "my key")

    assert raw_key.startswith("sk-")
    hashed = hash_api_key(raw_key)
    # Identity record written as a hash under ac:key:{hash}.
    assert fake._hashes[f"{_KEY_PREFIX}{hashed}"] == {"user_id": "u1", "description": "my key"}
    # Reverse user_id -> hash lookup written under ac:management:key:{user_id}.
    assert fake._strings[f"{_REVERSE_PREFIX}u1"] == hashed
    # The raw key round-trips back to the stored identity.
    identity = await provider.validate_token(raw_key)
    assert identity is not None
    assert identity.user_id == "u1"


async def test_provision_with_owner_stores_owner_claim(monkeypatch):
    """An owned key writes the owner claim into the identity record under the
    contract's shared claim key, so ``validate_token`` returns it in the claims."""
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)

    raw_key = await provider.provision("u1", "my key", owner_user_id="owner-7")

    hashed = hash_api_key(raw_key)
    assert fake._hashes[f"{_KEY_PREFIX}{hashed}"] == {
        "user_id": "u1",
        "description": "my key",
        OWNER_USER_ID_CLAIM: "owner-7",
    }
    # The owner claim reaches the identity claims for the backend's attenuation.
    identity = await provider.validate_token(raw_key)
    assert identity is not None
    assert identity.claims[OWNER_USER_ID_CLAIM] == "owner-7"


async def test_provision_without_owner_omits_owner_claim(monkeypatch):
    """An ownerless key omits the owner claim entirely — the field is absent from
    the stored hash, never present with a ``None`` value."""
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)

    raw_key = await provider.provision("u1", "my key")

    hashed = hash_api_key(raw_key)
    record = fake._hashes[f"{_KEY_PREFIX}{hashed}"]
    assert OWNER_USER_ID_CLAIM not in record
    assert record == {"user_id": "u1", "description": "my key"}


async def test_provision_duplicate_user_raises_and_leaves_first_record(monkeypatch):
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)

    first_raw = await provider.provision("u1", "first")
    first_hash = hash_api_key(first_raw)

    # A second provision for the same user is rejected atomically — it never writes a
    # second identity record that would be unreachable through the reverse lookup.
    with pytest.raises(DuplicateIdentityError):
        await provider.provision("u1", "second")

    assert fake._strings[f"{_REVERSE_PREFIX}u1"] == first_hash
    assert [k for k in fake._hashes if k.startswith(_KEY_PREFIX)] == [f"{_KEY_PREFIX}{first_hash}"]


async def test_provision_concurrent_write_aborts_exec_then_raises(monkeypatch):
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)

    # Simulate a concurrent client committing the reverse key inside the WATCH/EXEC
    # window: the first EXEC aborts (WatchError), and the retry re-reads and finds the
    # record now present, so provision raises rather than stranding a second document.
    def concurrent() -> None:
        fake._strings[f"{_REVERSE_PREFIX}u1"] = "concurrent-hash"

    fake.on_first_exec = concurrent
    with pytest.raises(DuplicateIdentityError):
        await provider.provision("u1", "desc")

    # Only the concurrent writer's reverse lookup survives; no orphan identity record
    # was written by the aborted provision.
    assert fake._strings[f"{_REVERSE_PREFIX}u1"] == "concurrent-hash"
    assert not [k for k in fake._hashes if k.startswith(_KEY_PREFIX)]


# -- revoke ------------------------------------------------------------------


async def test_revoke_deletes_record_and_reverse_lookup(monkeypatch):
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)
    raw_key = await provider.provision("u1", "d")
    hashed = hash_api_key(raw_key)

    assert await provider.revoke("u1") is True
    assert f"{_KEY_PREFIX}{hashed}" not in fake._hashes
    assert f"{_REVERSE_PREFIX}u1" not in fake._strings
    # The revoked key no longer authenticates.
    assert await provider.validate_token(raw_key) is None


async def test_revoke_unknown_user_returns_false(monkeypatch):
    assert await _provider(FakeRedis(), monkeypatch).revoke("nobody") is False


# -- update_description ------------------------------------------------------


async def test_update_description_rewrites_stored_description(monkeypatch):
    fake = FakeRedis()
    provider = _provider(fake, monkeypatch)
    raw_key = await provider.provision("u1", "old")
    hashed = hash_api_key(raw_key)

    assert await provider.update_description("u1", "new") is True
    record = fake._hashes[f"{_KEY_PREFIX}{hashed}"]
    # Only ``description`` is overwritten; ``user_id`` is left untouched.
    assert record == {"user_id": "u1", "description": "new"}


async def test_update_description_unknown_user_returns_false(monkeypatch):
    assert await _provider(FakeRedis(), monkeypatch).update_description("nobody", "x") is False


async def test_update_description_dangling_reverse_lookup_returns_false(monkeypatch):
    """A reverse lookup pointing at a missing identity document (a corrupted
    store) is a miss, not a crash: return ``False``."""
    fake = FakeRedis(strings={f"{_REVERSE_PREFIX}u1": "deadhash"})
    assert await _provider(fake, monkeypatch).update_description("u1", "x") is False


# -- list_identities ---------------------------------------------------------


async def test_list_identities_enumerates_stored_records(monkeypatch):
    fake = FakeRedis(
        hashes={
            f"{_KEY_PREFIX}h1": {"user_id": "u1", "description": "one"},
            f"{_KEY_PREFIX}h2": {"user_id": "u2", "description": "two"},
            # A record missing a user_id is skipped, not enumerated.
            f"{_KEY_PREFIX}h3": {"description": "orphan"},
            # An empty record (a key deleted between SCAN and read) is skipped too.
            f"{_KEY_PREFIX}h4": {},
        }
    )
    identities = await _provider(fake, monkeypatch).list_identities()
    assert sorted(identities) == [("u1", "one"), ("u2", "two")]


# -- healthcheck -------------------------------------------------------------


async def test_healthcheck_passes_on_reachable_store(monkeypatch):
    """A reachable plain Redis answers the ``HGETALL`` probe (missing key -> ``{}``);
    no raise."""
    assert await _provider(FakeRedis(), monkeypatch).healthcheck() is None


async def test_healthcheck_propagates_redis_errors(monkeypatch):
    """Any Redis error (e.g. auth failure) propagates as itself — a broken identity
    store fails startup loudly, never silently."""
    fake = FakeRedis(raise_hash=ResponseError("NOAUTH Authentication required"))
    with pytest.raises(ResponseError, match="NOAUTH"):
        await _provider(fake, monkeypatch).healthcheck()


# -- readiness_targets -------------------------------------------------------


def test_readiness_targets_declares_the_redis_record_store(monkeypatch):
    """The provider declares its own Redis record store as core's ``/ready`` target:
    the ``"access_control"`` label, kit's ``RedisClient``, and its redis settings."""
    redis_settings = object()  # opaque kit connection settings in production
    provider = RedisApiKeyProvider(_Settings(redis=redis_settings))

    (target,) = provider.readiness_targets()

    assert target.name == "access_control"
    assert target.client is RedisClient
    # The declared settings are the provider's own redis settings, not a copy.
    assert target.settings is redis_settings
