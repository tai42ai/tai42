"""The api-key lifecycle — mint, edit, re-mint and revoke orchestrated across the backends.

Every policy op lands in the Postgres store (the ``pg`` fake); the api-key IDENTITY
record is owned by a stub :class:`ApiKeyIdentityProvider`; and the live context + version
counter live on the ``FakeRedis``. These tests cover the fail-closed mint/revoke order,
the owner-required mint (every key belongs to a principal), the ``_UNSET`` PUT-edit split
across store and provider, the orphan re-mint, and the version bump each lifecycle writer
makes — not the store SQL (that is ``test_store.py``).
"""

from __future__ import annotations

import pytest
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_contract.template import TemplatedText

from tai42_skeleton.access_control import api_key_lifecycle, management, policy_version
from tai42_skeleton.access_control.settings import access_control_settings

from .conftest import ACL_MATCH, OWNER, FakeAccessControlPg, FakeRedis, SpyApiKeyProvider, make_client_ctx

S = access_control_settings()

pytestmark = pytest.mark.usefixtures("seed_owner")


def _ctx_key(user_id: str) -> str:
    return f"{S.context_prefix}{user_id}"


# -- mint ----------------------------------------------------------------------


async def test_mint_provisions_key_writes_policy_and_writes_no_context(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await management.add_url_to_scope("scope-a", "/a")
    raw_key, body, key_fingerprint = await api_key_lifecycle.add_user_api_key(
        "u1", "desc", ["scope-a"], owner_user_id=OWNER
    )
    assert raw_key == "sk-u1"
    # 1. provider owns the identity record; 2. policy row in PG. Mint writes NO
    # context — the live-context hash is created by the first counter write, so an
    # absent hash is the correct empty starting state.
    assert provider.identities == {"u1": "desc"}
    assert pg.policy("u1")["scopes"] == ["scope-a"]
    assert body["scopes"] == ["scope-a"]
    # Every mint stamps a fresh per-mint fingerprint into policy_data, returned alongside.
    assert key_fingerprint
    assert body["policy_data"][KEY_FINGERPRINT_CLAIM] == key_fingerprint
    assert _ctx_key("u1") not in redis._hashes


async def test_mint_requires_an_existing_enabled_owner_principal(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # Every api key belongs to a principal. An empty owner, an unknown owner, and a
    # disabled owner each raise loudly BEFORE the provider mints anything.
    with pytest.raises(ValueError, match="owner_user_id is required"):
        await api_key_lifecycle.add_user_api_key("k", "d", ["*"], owner_user_id="")
    with pytest.raises(ValueError, match="does not exist"):
        await api_key_lifecycle.add_user_api_key("k", "d", ["*"], owner_user_id="ghost")
    pg.add_principal("off", disabled=True)
    with pytest.raises(ValueError, match="disabled"):
        await api_key_lifecycle.add_user_api_key("k", "d", ["*"], owner_user_id="off")
    assert provider.provision_calls == []
    assert pg.policy("k") is None


async def test_mint_for_a_disabled_owner_admitted_only_when_the_caller_allows_it(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # The backup restore re-mints an archived key whose owner the archive holds disabled;
    # the owner stays disabled and the key mints onto it.
    pg.add_principal("off", disabled=True)
    raw_key, body, _fingerprint = await api_key_lifecycle.add_user_api_key(
        "k", "d", ["*"], owner_user_id="off", owner_may_be_disabled=True
    )
    assert raw_key == "sk-k"
    assert body["policy_data"][OWNER_USER_ID_CLAIM] == "off"
    assert pg.principal("off")["disabled"] is True
    # An unknown owner is refused whatever the caller allows.
    with pytest.raises(ValueError, match="does not exist"):
        await api_key_lifecycle.add_user_api_key("k2", "d", ["*"], owner_user_id="ghost", owner_may_be_disabled=True)


async def test_mint_rejects_unknown_scope_before_provisioning(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await api_key_lifecycle.add_user_api_key("u1", "desc", ["ghost"], owner_user_id=OWNER)
    # The pre-check fires BEFORE the provider mints anything — no half-provisioned key.
    assert provider.provision_calls == []
    assert pg.policy("u1") is None


async def test_mint_accepts_universal_wildcard_scope(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # "*" names no routed scope, so the typo guard must NOT 400 a wildcard mint: the
    # policy stores ["*"] and the mint still stamps a fresh per-mint fingerprint.
    raw_key, body, key_fingerprint = await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    assert raw_key == "sk-u1"
    assert pg.policy("u1")["scopes"] == ["*"]
    assert body["scopes"] == ["*"]
    assert key_fingerprint
    assert body["policy_data"][KEY_FINGERPRINT_CLAIM] == key_fingerprint


async def test_mint_still_rejects_a_typo_scope_alongside_the_wildcard(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # The wildcard skip does not disable the guard: a typo naming no route still raises.
    with pytest.raises(ValueError, match="does not exist"):
        await api_key_lifecycle.add_user_api_key("u1", "desc", ["*", "ghost"], owner_user_id=OWNER)
    assert provider.provision_calls == []
    assert pg.policy("u1") is None


async def test_mint_rejects_duplicate_user_before_provisioning(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    pg.add_policy("u1", scopes=[])
    with pytest.raises(ValueError, match="already in use"):
        await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    assert provider.provision_calls == []


async def test_mint_step2_failure_raises_and_leaves_key_policy_less(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # A policy-write failure AFTER the key is provisioned raises loudly, naming the
    # revoke-then-remint recovery. The key exists but is denied everything.
    pg.fault = ("INSERT INTO access_control_policies", RuntimeError("pg down"))
    with pytest.raises(RuntimeError, match="revoke_api_key"):
        await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    assert provider.identities == {"u1": "desc"}  # key provisioned
    assert pg.policy("u1") is None  # but no policy


async def test_revoke_then_remint_recovery_succeeds(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # Step-2 failure leaves the key policy-less; the documented recovery is
    # revoke-then-remint, which then succeeds with a fresh key.
    pg.fault = ("INSERT INTO access_control_policies", RuntimeError("pg down"))
    with pytest.raises(RuntimeError):
        await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    pg.fault = None
    assert await api_key_lifecycle.revoke_api_key("u1") is True
    raw_key, _body, _fingerprint = await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    assert raw_key == "sk-u1"
    assert pg.policy("u1") is not None


async def test_revoke_then_remint_mints_a_fresh_fingerprint(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # A revoke deletes the policy row, fingerprint and all, so a remint of the same
    # user_id writes a brand-new one that no old binding can match.
    _raw, _body, first = await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    assert await api_key_lifecycle.revoke_api_key("u1") is True
    _raw2, body2, second = await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    assert first != second
    assert body2["policy_data"][KEY_FINGERPRINT_CLAIM] == second


async def test_plain_remint_of_same_user_raises_duplicate(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    # NOT idempotent: a plain retry hits the duplicate-user guard.
    with pytest.raises(ValueError, match="already in use"):
        await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)


async def test_owner_threaded_to_provision_and_written_to_policy(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    pg.add_principal("owner-1", kind="service", display_name="svc")
    _raw, body, _fingerprint = await api_key_lifecycle.add_user_api_key("k1", "desc", ["*"], owner_user_id="owner-1")
    # Owner reaches the provider (identity-claim home) as a keyword arg ...
    assert provider.provision_owners["k1"] == "owner-1"
    # ... and is dual-homed into the committed policy_data (management/listing home).
    assert body["policy_data"][OWNER_USER_ID_CLAIM] == "owner-1"
    assert pg.policy("k1")["policy_data"][OWNER_USER_ID_CLAIM] == "owner-1"


# -- revoke --------------------------------------------------------------------


async def test_revoke_kills_policy_first_then_context_then_key(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    # An external metering writer accrued live counters into the context hash.
    redis._hashes[_ctx_key("u1")] = {"used": "9"}
    assert await api_key_lifecycle.revoke_api_key("u1") is True
    # Policy row gone (PG), key record gone (provider), context hash deleted (Redis).
    assert pg.policy("u1") is None
    assert provider.identities == {}
    assert _ctx_key("u1") not in redis._hashes


async def test_revoke_bumps_the_policy_version_with_the_policy_delete(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis, monkeypatch
) -> None:
    # The policy cache is keyed on (user_id, version), so a warm slot keeps serving the
    # revoked key until the version moves: the bump must ride with the policy-row delete.
    settings = access_control_settings()
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    before = redis._strings.get(settings.policy_version_key)

    async def _boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "delete", _boom)
    with pytest.raises(RuntimeError, match="redis down"):
        await api_key_lifecycle.revoke_api_key("u1")

    assert pg.policy("u1") is None
    assert int(redis._strings[settings.policy_version_key]) > int(before or 0)


async def test_revoke_unknown_user_returns_false(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    assert await api_key_lifecycle.revoke_api_key("missing") is False


async def test_revoke_leaves_a_policy_row_that_was_never_a_key_untouched(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # Role assignment provisions policy rows this surface does not own; the identity
    # record is the only existence signal of a MINTED key.
    pg.add_policy("account-user", scopes=["hooks"])
    assert await api_key_lifecycle.revoke_api_key("account-user") is False
    assert pg.policy("account-user") is not None


async def test_revoke_failed_policy_delete_raises_leaving_the_key_authority_less(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    pg.fault = ("DELETE FROM access_control_policies", RuntimeError("pg down"))
    with pytest.raises(RuntimeError, match="pg down"):
        await api_key_lifecycle.revoke_api_key("u1")
    # The authority a fire runs on dies FIRST, so a failure there leaves the key untouched
    # rather than an identity-less policy row no retry can reach.
    assert provider.identities == {"u1": "desc"}
    assert pg.policy("u1") is not None


async def test_revoke_retry_after_a_failed_policy_delete_completes(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # The identity record (the existence signal) is deleted LAST, so a retry finishes the
    # revocation instead of answering 404 over a row that still carries fire authority.
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    pg.fault = ("DELETE FROM access_control_policies", RuntimeError("pg down"))
    with pytest.raises(RuntimeError, match="pg down"):
        await api_key_lifecycle.revoke_api_key("u1")

    pg.fault = None
    assert await api_key_lifecycle.revoke_api_key("u1") is True
    assert pg.policy("u1") is None
    assert provider.identities == {}


async def test_revoke_failed_context_delete_raises(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, monkeypatch
) -> None:
    plain = FakeRedis(strings={})
    monkeypatch.setattr(api_key_lifecycle, "client_ctx", make_client_ctx(plain))
    monkeypatch.setattr(policy_version, "client_ctx", make_client_ctx(plain))
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    # Now make the context delete fail: an orphaned context hash would corrupt a
    # future remint, so the failure must surface loudly.
    broken = FakeRedis(hashes={_ctx_key("u1"): {"used": "1"}})

    async def _boom(*_a, **_k):
        raise RuntimeError("redis down")

    broken.delete = _boom  # type: ignore[method-assign]
    monkeypatch.setattr(api_key_lifecycle, "client_ctx", make_client_ctx(broken))
    monkeypatch.setattr(policy_version, "client_ctx", make_client_ctx(broken))
    with pytest.raises(RuntimeError, match="redis down"):
        await api_key_lifecycle.revoke_api_key("u1")
    # The identity record outlives the fault, so the retry reaches the context delete
    # instead of leaving a hash that would corrupt the next remint of the same id.
    assert provider.identities == {"u1": "desc"}
    monkeypatch.setattr(api_key_lifecycle, "client_ctx", make_client_ctx(plain))
    monkeypatch.setattr(policy_version, "client_ctx", make_client_ctx(plain))
    plain._hashes[_ctx_key("u1")] = {"used": "1"}
    assert await api_key_lifecycle.revoke_api_key("u1") is True
    assert _ctx_key("u1") not in plain._hashes
    assert provider.identities == {}


async def test_remint_after_revoke_starts_with_fresh_context(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    redis._hashes[_ctx_key("u1")] = {"used": "99"}  # simulate accrued counters
    await api_key_lifecycle.revoke_api_key("u1")
    assert _ctx_key("u1") not in redis._hashes
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    # A remint of the reused id starts fresh: no seed, no hash — an absent hash is
    # the empty live view, never inheriting the dead key's counters.
    assert _ctx_key("u1") not in redis._hashes


# -- edit (PUT split across provider + store) ----------------------------------


async def test_edit_splits_description_to_provider_and_policy_to_store(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await management.add_url_to_scope("scope-b", "/b")
    await api_key_lifecycle.add_user_api_key("u1", "old-desc", ["*"], owner_user_id=OWNER)
    updated = await api_key_lifecycle.edit_user_payload("u1", description="new-desc", scopes=["scope-b"])
    assert updated is not None
    assert updated["scopes"] == ["scope-b"]  # policy → store
    assert provider.identities["u1"] == "new-desc"  # description → provider
    assert provider.description_calls == [("u1", "new-desc")]


async def test_edit_description_only_leaves_policy_untouched(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    _raw, _body, fingerprint = await api_key_lifecycle.add_user_api_key(
        "u1", "old", ["*"], policy_data={"k": 1}, condition=TemplatedText(content=".c"), owner_user_id=OWNER
    )
    updated = await api_key_lifecycle.edit_user_payload("u1", description="new")
    assert updated is not None
    # A description-only edit preserves every stored policy field (returns them),
    # including the per-mint fingerprint and the owner claim the mint stamped into policy_data.
    assert updated == {
        "scopes": ["*"],
        "policy_data": {"k": 1, KEY_FINGERPRINT_CLAIM: fingerprint, OWNER_USER_ID_CLAIM: OWNER},
        "condition": {"content": ".c"},
    }
    assert provider.identities["u1"] == "new"


async def test_edit_scopes_only_never_touches_provider(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await management.add_url_to_scope("scope-b", "/b")
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    await api_key_lifecycle.edit_user_payload("u1", scopes=["scope-b"])
    # description left _UNSET → the provider is never called.
    assert provider.description_calls == []


async def test_edit_explicit_null_clears_policy_fields(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    _raw, _body, fingerprint = await api_key_lifecycle.add_user_api_key(
        "u1", "desc", ["*"], policy_data={"k": 1}, condition=TemplatedText(content=".c"), owner_user_id=OWNER
    )
    updated = await api_key_lifecycle.edit_user_payload("u1", policy_data=None, condition=None)
    assert updated is not None
    # The explicit clear drops the caller's policy_data but preserves the server-owned,
    # immutable anchors an edit never touches: the per-mint fingerprint and the owner claim.
    assert updated["policy_data"] == {KEY_FINGERPRINT_CLAIM: fingerprint, OWNER_USER_ID_CLAIM: OWNER}
    assert updated["condition"] is None


async def test_edit_unknown_user_returns_none_without_provider_call(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    assert await api_key_lifecycle.edit_user_payload("missing", description="x") is None
    assert provider.description_calls == []


async def test_edit_rejects_unknown_scope(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "desc", ["*"], owner_user_id=OWNER)
    with pytest.raises(ValueError, match="does not exist"):
        await api_key_lifecycle.edit_user_payload("u1", scopes=["ghost"])


async def test_edit_missing_identity_while_a_policy_row_exists_raises(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # A policy row with no matching identity record is orphaned: a supplied description
    # that the provider cannot find raises loudly, naming the orphaned state.
    pg.add_policy("u1", scopes=[])
    with pytest.raises(RuntimeError, match="orphaned policy row"):
        await api_key_lifecycle.edit_user_payload("u1", description="x")


# -- orphaned policy rows (partial-restore state) ------------------------------


async def test_revoke_clears_an_orphan_without_calling_the_provider(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    pg.add_policy("orphan", scopes=[], policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    redis._hashes[_ctx_key("orphan")] = {"used": "3"}
    # An orphan has no identity record to revoke: the policy row and context hash are
    # cleared, True is returned, and the provider is never called.
    assert await api_key_lifecycle.revoke_api_key("orphan") is True
    assert pg.policy("orphan") is None
    assert _ctx_key("orphan") not in redis._hashes
    assert provider.revoke_calls == []


async def test_mint_on_an_orphaned_id_raises_naming_the_orphan_case(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    pg.add_policy("orphan", scopes=[], policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    with pytest.raises(ValueError, match="orphaned policy row"):
        await api_key_lifecycle.add_user_api_key("orphan", "d", ["*"], owner_user_id=OWNER)
    # No auto-heal: the provider mints nothing on a plain mint over an orphan.
    assert provider.provision_calls == []


async def test_remint_orphan_rehomes_the_owner_and_leaves_the_policy_byte_equal(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    owner = "owner-1"
    policy_data = {KEY_FINGERPRINT_CLAIM: "fp-1", OWNER_USER_ID_CLAIM: owner}
    body_before = {"scopes": ["s"], "policy_data": policy_data, "condition": None}
    pg.add_policy("orphan", scopes=["s"], policy_data=dict(policy_data))

    raw = await api_key_lifecycle.remint_orphaned_api_key("orphan", "restored")
    assert raw == "sk-orphan"
    # A fresh identity record exists, carrying the owner claim re-homed from the policy.
    assert provider.identities["orphan"] == "restored"
    assert provider.provision_owners["orphan"] == owner
    # The policy row is untouched: scopes, condition, fingerprint and owner all byte-equal.
    assert pg.policy_body("orphan") == body_before


async def test_remint_orphan_with_no_owner_claim_raises(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    # A minted policy row with no owner claim is an ownerless key row: a re-mint has no
    # principal to re-home onto, so it raises loudly.
    pg.add_policy("orphan", scopes=["s"], policy_data={KEY_FINGERPRINT_CLAIM: "fp-1"})
    with pytest.raises(ValueError, match="ownerless key row"):
        await api_key_lifecycle.remint_orphaned_api_key("orphan", "restored")
    assert provider.provision_calls == []


async def test_remint_refuses_a_non_orphaned_state(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis
) -> None:
    await api_key_lifecycle.add_user_api_key("live", "d", ["*"], owner_user_id=OWNER)
    with pytest.raises(ValueError, match="not an orphaned"):
        await api_key_lifecycle.remint_orphaned_api_key("live", "x")
    # No second provision on the refusal (only the original live mint).
    assert provider.provision_calls == ["live"]


# -- lifecycle writers invalidate the policy cache -----------------------------


async def test_add_user_api_key_bumps_once_after_the_policy_write(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, bumps: list[int]
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "d", ["*"], owner_user_id=OWNER)
    assert bumps == [1]


async def test_add_user_api_key_refused_does_not_bump(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, bumps: list[int]
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await api_key_lifecycle.add_user_api_key("u1", "d", ["scope-ghost"], owner_user_id=OWNER)
    assert bumps == []


async def test_edit_user_payload_bumps_once_and_not_for_an_unknown_user(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, bumps: list[int]
) -> None:
    assert await api_key_lifecycle.edit_user_payload("ghost", scopes=[]) is None
    assert bumps == []
    await api_key_lifecycle.add_user_api_key("u1", "d", ["*"], owner_user_id=OWNER)
    bumps.clear()
    assert await api_key_lifecycle.edit_user_payload("u1", condition=None) is not None
    assert bumps == [1]


async def test_revoke_bumps_immediately_inside_a_batch(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis, bumps: list[int]
) -> None:
    await api_key_lifecycle.add_user_api_key("u1", "d", ["*"], owner_user_id=OWNER)
    bumps.clear()
    async with policy_version.policy_write_batch():
        await api_key_lifecycle.revoke_api_key("u1")
        # The revoke's fail-closed order needs its bump now, not at the batch's end.
        assert bumps == [1]
    assert bumps == [1]


# -- the stored template id a condition names ----------------------------------


async def test_mint_refuses_a_condition_id_the_store_lacks(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis, store_holding
) -> None:
    store_holding()
    with pytest.raises(ValueError, match=ACL_MATCH):
        await api_key_lifecycle.add_user_api_key(
            "u1", "d", [], condition=TemplatedText(id="jq/acl"), owner_user_id=OWNER
        )
    assert provider.identities == {}
    assert pg.policy("u1") is None


async def test_edit_refuses_a_supplied_condition_id_the_store_lacks(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis, store_holding
) -> None:
    store_holding()
    await api_key_lifecycle.add_user_api_key("u1", "d", ["*"], owner_user_id=OWNER)
    with pytest.raises(ValueError, match=ACL_MATCH):
        await api_key_lifecycle.edit_user_payload("u1", condition=TemplatedText(id="jq/acl"))
    assert pg.policy("u1")["condition"] is None


async def test_an_edit_supplying_no_condition_does_not_read_the_stored_one(
    pg: FakeAccessControlPg, provider: SpyApiKeyProvider, redis: FakeRedis, store_holding
) -> None:
    storage = store_holding("jq/acl")
    await api_key_lifecycle.add_user_api_key("u1", "d", [], condition=TemplatedText(id="jq/acl"), owner_user_id=OWNER)
    storage.items.clear()
    updated = await api_key_lifecycle.edit_user_payload("u1", policy_data={"tier": "x"})
    assert updated is not None
    assert updated["condition"] == {"id": "jq/acl"}
