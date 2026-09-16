"""The host-side setup recovery — re-mint one surviving owner key behind the setup gate.

Covers ``recover_owner_key``: the gate order it shares with the setup door (access-control-off
refusal, the ``local`` throttle before the compare, a generic ``Forbidden`` on a wrong token
with the ``local`` failure counter incremented, the no-mint-provider refusal, the shared mint
mutex), the owner-marker resolution (the unique ``created_by IS NULL`` principal; not-initialized
and invariant-breach refusals), the owner-key candidate filter (only the owner's minted rows,
never another principal's), the live-key and no-candidate refusals, the row choice
(``--key-user``, the sole candidate, or a name-one refusal), and the happy path (a fresh
identity record onto the untouched policy row, the plaintext returned once, failures cleared).
"""

from __future__ import annotations

import copy

import pytest
from pydantic import SecretStr
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM, registry

from tai42_skeleton.access_control import management, setup_recovery
from tai42_skeleton.access_control import setup_gate as setup_gate_mod
from tai42_skeleton.access_control.settings import access_control_settings, setup_settings
from tai42_skeleton.access_control.setup_recovery import SetupRecoveryError, recover_owner_key

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx
from .test_setup import _SpyProvider, _ValidatorProvider

_TOKEN = "setup-token-xyz"


@pytest.fixture
def provider() -> _SpyProvider:
    spy = _SpyProvider()
    registry._REGISTRY["redis"] = lambda _settings: spy
    return spy


@pytest.fixture
def setup_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """The AC Redis the gate's throttle counters and mint mutex live on."""
    fake = FakeRedis(strings={})
    monkeypatch.setattr(setup_gate_mod, "client_ctx", make_client_ctx(fake))
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(fake))
    return fake


@pytest.fixture
def operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin an operator-set setup token, gate on, so the token resolves without a Redis auto-token."""
    monkeypatch.setattr(setup_settings(), "token", SecretStr(_TOKEN))
    monkeypatch.setattr(setup_settings(), "open", False)
    monkeypatch.setattr(access_control_settings(), "enable", True)


def _seed_owner(pg: FakeAccessControlPg) -> None:
    """The owner principal (the ``created_by IS NULL`` marker) plus its own admin account row."""
    pg.add_principal("owner", kind="human", display_name="Owner", created_by=None)
    pg.add_policy("owner", scopes=["*"], policy_data={})


def _seed_orphan_key(pg: FakeAccessControlPg, user_id: str, *, owner: str, fingerprint: str) -> None:
    """A minted key policy row (fingerprint + owner claim) with no identity record — an orphan."""
    pg.add_policy(
        user_id,
        scopes=["*"],
        policy_data={KEY_FINGERPRINT_CLAIM: fingerprint, OWNER_USER_ID_CLAIM: owner},
    )


def _fail_key() -> str:
    settings = access_control_settings()
    return f"{settings.setup_throttle_fail_prefix}{setup_recovery.LOCAL_SOURCE}"


async def test_recover_not_initialized_points_at_setup(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    with pytest.raises(SetupRecoveryError, match="not initialized; run `tai setup`"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == {}


async def test_recover_wrong_token_is_forbidden_and_counts_a_local_failure(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    with pytest.raises(SetupRecoveryError, match="Forbidden"):
        await recover_owner_key("nope", key_user_id=None, key_description="recovered")
    # The failure counter is bucketed under the ``local`` source, and nothing is minted.
    assert setup_redis._strings.get(_fail_key()) == "1"
    assert provider.identities == {}


async def test_recover_over_threshold_throttles_before_compare(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    operator_token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    monkeypatch.setattr(access_control_settings(), "setup_throttle_threshold", 2)
    for _ in range(3):
        with pytest.raises(SetupRecoveryError, match="Forbidden"):
            await recover_owner_key("wrong", key_user_id=None, key_description="recovered")
    assert await setup_gate_mod.setup_throttle_locked(setup_recovery.LOCAL_SOURCE) is True
    # While locked, even the CORRECT token is turned away without minting (no oracle, no bypass).
    with pytest.raises(SetupRecoveryError, match="Forbidden"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == {}


async def test_recover_refuses_when_owner_has_a_live_key(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    # A live identity record for the owner's key: recovery does not apply.
    provider.identities["owner-key"] = "live key"
    before = dict(provider.identities)
    with pytest.raises(SetupRecoveryError, match="already holds a live key \\(owner-key\\)"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == before  # provider untouched — nothing re-minted


async def test_recover_remints_the_sole_orphan_owner_key(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    # A pre-existing failure counter proves the success path clears it.
    setup_redis._strings[_fail_key()] = "1"
    row_before = copy.deepcopy(pg.policy_body("owner-key"))

    result = await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")

    assert result["owner_user_id"] == "owner"
    assert result["key_user_id"] == "owner-key"
    assert result["api_key"] == "sk-owner-key"
    assert result["key_fingerprint"] == "fp-1"
    # A fresh identity record for the SAME id, owned by the owner, minted exactly once.
    assert provider.identities == {"owner-key": "recovered"}
    assert provider.provision_owners == {"owner-key": "owner"}
    # The policy row is byte-equal: scopes, condition, fingerprint and owner claim all intact.
    assert pg.policy_body("owner-key") == row_before
    # Failures for the ``local`` source are cleared after a successful recovery.
    assert _fail_key() not in setup_redis._strings


async def test_recover_lists_both_when_several_owner_keys(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-k1", owner="owner", fingerprint="fp-1")
    _seed_orphan_key(pg, "owner-k2", owner="owner", fingerprint="fp-2")
    with pytest.raises(SetupRecoveryError, match="several keys \\(owner-k1, owner-k2\\); name the one"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == {}


async def test_recover_key_user_selects_one_owner_key(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-k1", owner="owner", fingerprint="fp-1")
    _seed_orphan_key(pg, "owner-k2", owner="owner", fingerprint="fp-2")
    result = await recover_owner_key(_TOKEN, key_user_id="owner-k2", key_description="recovered")
    assert result["key_user_id"] == "owner-k2"
    assert result["key_fingerprint"] == "fp-2"
    assert provider.identities == {"owner-k2": "recovered"}


async def test_recover_key_user_not_an_owner_key_is_refused(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    with pytest.raises(SetupRecoveryError, match="--key-user 'ghost' is not one of the owner's keys \\(owner-key\\)"):
        await recover_owner_key(_TOKEN, key_user_id="ghost", key_description="recovered")
    assert provider.identities == {}


async def test_recover_never_touches_another_principals_orphan_key(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    # A service principal (created by the owner) with its OWN orphaned key.
    pg.add_principal("svc", kind="service", display_name="Service", created_by="owner")
    _seed_orphan_key(pg, "svc-key", owner="svc", fingerprint="fp-svc")

    result = await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")

    assert result["key_user_id"] == "owner-key"
    # Only the owner's key is re-minted; the service principal's orphan stays orphaned.
    assert provider.identities == {"owner-key": "recovered"}
    assert await management.api_key_state("svc-key") == "orphaned"


async def test_recover_refuses_when_no_surviving_owner_key(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)  # owner exists, but no minted key row survives
    with pytest.raises(SetupRecoveryError, match="no surviving key policy row to re-mint"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == {}


async def test_recover_refuses_on_ambiguous_owner_marker(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    pg.add_principal("owner-a", kind="human", display_name="A", created_by=None)
    pg.add_principal("owner-b", kind="human", display_name="B", created_by=None)
    with pytest.raises(SetupRecoveryError, match="several owner principals \\(created_by NULL\\): owner-a, owner-b"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == {}


async def test_recover_refuses_when_gate_is_off(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_control_settings(), "enable", False)
    monkeypatch.setattr(setup_settings(), "token", SecretStr(_TOKEN))
    with pytest.raises(SetupRecoveryError, match="access control is off"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")


async def test_recover_refuses_when_no_provider_can_mint(
    pg: FakeAccessControlPg, setup_redis: FakeRedis, operator_token: None
) -> None:
    registry._REGISTRY["redis"] = lambda _settings: _ValidatorProvider()
    _seed_owner(pg)
    with pytest.raises(SetupRecoveryError, match="no configured identity provider can mint"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")


async def test_recover_refuses_when_mint_lock_is_contended(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None
) -> None:
    _seed_owner(pg)
    _seed_orphan_key(pg, "owner-key", owner="owner", fingerprint="fp-1")
    # A concurrent setup already holds the mint mutex.
    setup_redis._strings[access_control_settings().setup_lock_key] = "held"
    with pytest.raises(SetupRecoveryError, match="a concurrent setup holds the mint lock; retry"):
        await recover_owner_key(_TOKEN, key_user_id=None, key_description="recovered")
    assert provider.identities == {}
