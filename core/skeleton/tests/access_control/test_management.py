"""The management provisioning surface — scope/route and principal CRUD over the policy store.

Every policy op delegates to the Postgres store (the ``pg`` fake); the api-key IDENTITY
records are owned by a stub :class:`ApiKeyIdentityProvider`; and the version counter
lives on the ``FakeRedis``. These tests cover the first-mintable provider resolution,
the tokens-payload merge, the provisioning-state classification, and the version bump
each CRUD writer makes — not the store SQL (that is ``test_store.py``).
"""

from __future__ import annotations

import pytest
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider
from tai42_kit.access_control import registry
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.access_control import api_key_lifecycle, management, policy_version
from tai42_skeleton.access_control.settings import access_control_settings

from .conftest import ACL_MATCH, OWNER, FakeAccessControlPg, FakeRedis, SpyApiKeyProvider, make_client_ctx

S = access_control_settings()

pytestmark = pytest.mark.usefixtures("seed_owner")


class _ValidatorProvider(IdentityProvider):
    """A non-mintable provider (validates tokens only); never provisions keys."""

    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused here
        return None


# -- first-mintable resolution -------------------------------------------------


async def test_first_mintable_provider_chosen(pg: FakeAccessControlPg, redis: FakeRedis, monkeypatch) -> None:
    # A validator-only provider first in the chain is skipped; the second, mintable
    # provider is chosen to provision.
    spy = SpyApiKeyProvider()
    registry._PROVIDERS._generation.committed()["validator"] = lambda _s: _ValidatorProvider()
    registry._PROVIDERS._generation.committed()["mintable"] = lambda _s: spy
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["validator", "mintable"]')
    reset_all_settings()
    try:
        raw_key, _body, _fingerprint = await api_key_lifecycle.add_user_api_key(
            "u1", "desc", ["*"], owner_user_id=OWNER
        )
        assert raw_key == "sk-u1"
        assert spy.provision_calls == ["u1"]
    finally:
        reset_all_settings()


async def test_no_mintable_provider_raises_typeerror(pg: FakeAccessControlPg, monkeypatch) -> None:
    registry._PROVIDERS._generation.committed()["validator"] = lambda _s: _ValidatorProvider()
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["validator"]')
    reset_all_settings()
    try:
        with pytest.raises(TypeError, match="no configured identity provider"):
            await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    finally:
        reset_all_settings()


async def test_provider_capabilities_reports_mintability(monkeypatch) -> None:
    registry._PROVIDERS._generation.committed()["validator"] = lambda _s: _ValidatorProvider()
    registry._PROVIDERS._generation.committed()["mintable"] = lambda _s: SpyApiKeyProvider()
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["validator", "mintable"]')
    reset_all_settings()
    try:
        assert management.provider_capabilities() == [("validator", False), ("mintable", True)]
    finally:
        reset_all_settings()


# -- tokens payload (identity + policy merge) ----------------------------------


async def test_tokens_payload_merges_identity_and_policy(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    _raw, _body, fingerprint = await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    pg.policy("u1")["scopes"] = ["scope-a"]
    payload = await management.get_all_existing_tokens_payload()
    assert payload == [
        {
            "user_id": "u1",
            "description": "desc",
            "scopes": ["scope-a"],
            "policy_data": {KEY_FINGERPRINT_CLAIM: fingerprint, OWNER_USER_ID_CLAIM: OWNER},
            "condition": None,
            "orphaned": False,
        }
    ]


async def test_tokens_payload_skips_reserved_and_falsy(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    provider.identities = {"__root__": "root", "": "blank", "u1": "desc"}
    payload = await management.get_all_existing_tokens_payload()
    assert [p["user_id"] for p in payload] == ["u1"]


async def test_tokens_payload_empty_on_validator_only_deployment(pg: FakeAccessControlPg, monkeypatch) -> None:
    # A validator-only chain has no mint-capable provider, so there are no api-keys to
    # enumerate: the payload is empty.
    registry._PROVIDERS._generation.committed()["validator"] = lambda _s: _ValidatorProvider()
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["validator"]')
    reset_all_settings()
    try:
        assert await management.get_all_existing_tokens_payload() == []
    finally:
        reset_all_settings()


# -- orphaned policy rows (partial-restore state) ------------------------------


async def test_tokens_payload_flags_orphaned_minted_rows(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # A live key (identity + minted policy), an orphan (minted policy, no identity),
    # and an account row (no fingerprint, no identity — never a key).
    await api_key_lifecycle.add_user_api_key("live", "live-desc", ["*"], owner_user_id=OWNER)
    pg.add_policy("orphan", scopes=["s"], policy_data={KEY_FINGERPRINT_CLAIM: "fp-x"})
    pg.add_policy("account", scopes=["hooks"])

    by_id = {row["user_id"]: row for row in await management.get_all_existing_tokens_payload()}
    # A live key carries orphaned=False; the orphan is surfaced flagged and description-less
    # with its policy intact; the account row is not enumerated at all.
    assert by_id["live"]["orphaned"] is False
    assert by_id["orphan"]["orphaned"] is True
    assert by_id["orphan"]["description"] == ""
    assert by_id["orphan"]["scopes"] == ["s"]
    assert by_id["orphan"]["policy_data"] == {KEY_FINGERPRINT_CLAIM: "fp-x"}
    assert "account" not in by_id


async def test_api_key_state_reads_the_four_states(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    assert await management.api_key_state("nobody") == "absent"
    await api_key_lifecycle.add_user_api_key("live", "d", ["*"], owner_user_id=OWNER)
    assert await management.api_key_state("live") == "live"
    pg.add_policy("orphan", scopes=[], policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    assert await management.api_key_state("orphan") == "orphaned"
    pg.add_policy("account", scopes=["hooks"])
    assert await management.api_key_state("account") == "account"


async def test_null_fingerprint_value_is_an_account_row_not_a_minted_one(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # A JSON null under the fingerprint claim is NOT a minted key: the marker is a
    # non-null value. The row is an account row — never listed as a token, and its state
    # is ``account``, not ``orphaned``.
    pg.add_policy("null-fp", scopes=["s"], policy_data={KEY_FINGERPRINT_CLAIM: None})
    assert [row["user_id"] for row in await management.get_all_existing_tokens_payload()] == []
    assert await management.api_key_state("null-fp") == "account"


# -- version bump --------------------------------------------------------------


async def test_bump_policy_version_increments(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    assert await policy_version.bump_policy_version() == 1
    assert await policy_version.bump_policy_version() == 2


async def test_bump_policy_version_with_the_gate_off_touches_no_redis(monkeypatch) -> None:
    # Nothing reads the policy version while access control is off, so the bump is a no-op:
    # it opens no Redis client (the real factory would raise with no URL configured).
    from tai42_kit.clients import client_ctx
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    monkeypatch.delenv("ACCESS_CONTROL_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setattr(policy_version, "client_ctx", client_ctx)
    reset_all_settings()
    assert await policy_version.bump_policy_version() is None


async def test_failed_version_bump_raises(pg: FakeAccessControlPg, provider: SpyApiKeyProvider, monkeypatch) -> None:
    fake = FakeRedis(strings={})

    async def _boom(*_a, **_k):
        raise RuntimeError("redis down")

    fake.incr = _boom  # type: ignore[method-assign]
    monkeypatch.setattr(policy_version, "client_ctx", make_client_ctx(fake))
    with pytest.raises(RuntimeError, match="redis down"):
        await policy_version.bump_policy_version()


# -- writers invalidate the policy cache ---------------------------------------


async def test_add_url_to_scope_bumps_once(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    await management.add_url_to_scope("scope-a", "/a")
    assert bumps == [1]


async def test_remove_url_from_scope_bumps_once_when_it_existed(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    await management.add_url_to_scope("scope-a", "/a")
    bumps.clear()
    existed, _ = await management.remove_url_from_scope("/a")
    assert existed is True
    assert bumps == [1]


async def test_remove_url_from_scope_does_not_bump_on_a_no_op(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    existed, _ = await management.remove_url_from_scope("/never-mapped")
    assert existed is False
    assert bumps == []


async def test_remove_scope_bumps_once_when_something_was_deleted(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    await management.add_url_to_scope("scope-a", "/a")
    bumps.clear()
    deleted, _ = await management.remove_scope("scope-a")
    assert deleted > 0
    assert bumps == [1]


async def test_remove_scope_does_not_bump_on_a_no_op(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    deleted, _ = await management.remove_scope("scope-ghost")
    assert deleted == 0
    assert bumps == []


async def test_pin_route_public_bumps_once(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    await management.pin_route_public("/open")
    assert bumps == [1]


async def test_pin_route_public_refused_does_not_bump(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    reserved = S.reserved_public_pin_prefixes[0]
    with pytest.raises(ValueError, match="reserved"):
        await management.pin_route_public(f"{reserved}/x")
    assert bumps == []


async def test_unpin_public_route_bumps_only_when_it_was_pinned(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    assert await management.unpin_public_route("/open") is False
    assert bumps == []
    await management.pin_route_public("/open")
    bumps.clear()
    assert await management.unpin_public_route("/open") is True
    assert bumps == [1]


async def test_restore_policy_body_bumps_only_when_restored(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, bumps: list[int]
) -> None:
    assert await management.restore_policy_body("ghost", {"scopes": [], "policy_data": {}, "condition": None}) is None
    assert bumps == []
    _, body, _ = await api_key_lifecycle.add_user_api_key("u1", "d", ["*"], owner_user_id=OWNER)
    bumps.clear()
    assert await management.restore_policy_body("u1", body) is not None
    assert bumps == [1]


async def test_policy_write_batch_bumps_once_for_many_writes(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    async with policy_version.policy_write_batch():
        await management.add_url_to_scope("scope-a", "/a")
        await management.add_url_to_scope("scope-b", "/b")
        await management.pin_route_public("/open")
        assert bumps == []
    assert bumps == [1]


async def test_policy_write_batch_without_a_change_does_not_bump(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    async with policy_version.policy_write_batch():
        await management.unpin_public_route("/never-pinned")
    assert bumps == []


async def test_policy_write_batch_bumps_when_the_body_raises(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    async def _restore_that_raises() -> None:
        async with policy_version.policy_write_batch():
            await management.add_url_to_scope("scope-a", "/a")
            raise RuntimeError("mid-restore")

    with pytest.raises(RuntimeError, match="mid-restore"):
        await _restore_that_raises()
    # What was written before the raise is still invalidated.
    assert bumps == [1]


async def test_policy_write_batch_is_not_reentrant(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    async with policy_version.policy_write_batch():
        with pytest.raises(RuntimeError, match="not reentrant"):
            async with policy_version.policy_write_batch():
                pass


async def test_writes_after_a_batch_bump_on_their_own(pg: FakeAccessControlPg, bumps: list[int]) -> None:
    async with policy_version.policy_write_batch():
        await management.add_url_to_scope("scope-a", "/a")
    await management.add_url_to_scope("scope-b", "/b")
    assert bumps == [1, 1]


# -- the stored template id a condition names ----------------------------------


async def test_create_principal_policy_refuses_a_condition_id_the_store_lacks(
    pg: FakeAccessControlPg, redis: FakeRedis, store_holding
) -> None:
    store_holding()
    with pytest.raises(ValueError, match=ACL_MATCH):
        await management.create_principal_policy("p1", [], None, {"id": "jq/acl"})
    assert pg.policy("p1") is None


async def test_restore_policy_body_refuses_a_condition_id_the_store_lacks(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis, store_holding
) -> None:
    store_holding()
    await api_key_lifecycle.add_user_api_key("u1", "d", ["*"], owner_user_id=OWNER)
    body = {**pg.policy("u1"), "condition": {"id": "jq/acl"}}
    with pytest.raises(ValueError, match=ACL_MATCH):
        await management.restore_policy_body("u1", body)
    assert pg.policy("u1")["condition"] is None
