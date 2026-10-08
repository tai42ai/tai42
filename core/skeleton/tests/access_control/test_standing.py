"""``resolve_standing``: one order every door reads a principal in, and the jq passes built from it."""

from __future__ import annotations

import logging

import pytest
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_contract.template import TemplatedText

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control.policy import PolicyEnforcer
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.standing import (
    Standing,
    StandingDenied,
    StandingDenyReason,
    jq_passes,
    resolve_standing,
)

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")


@pytest.fixture
def enforcer(monkeypatch: pytest.MonkeyPatch, pg: FakeAccessControlPg) -> PolicyEnforcer:
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    return PolicyEnforcer(AccessControlSettings())


async def _denied(enforcer: PolicyEnforcer, user_id: str, **kwargs) -> StandingDenied:
    with pytest.raises(StandingDenied) as caught:
        await resolve_standing(enforcer, user_id, version=0, **kwargs)
    return caught.value


async def test_a_principal_with_no_policy_has_no_standing(enforcer):
    denied = await _denied(enforcer, "ghost", verified_claims=None)
    assert (denied.reason, denied.principal, denied.subject) == (StandingDenyReason.NO_POLICY, "ghost", "ghost")


async def test_a_disabled_principal_has_no_standing(enforcer, pg):
    pg.add_policy("u1", scopes=["a"], policy_data={"disabled": True})
    denied = await _denied(enforcer, "u1", verified_claims=None)
    assert (denied.reason, denied.subject) == (StandingDenyReason.DISABLED, "u1")


async def test_an_unowned_principal_stands_on_its_own_policy(enforcer, pg):
    pg.add_policy("u1", scopes=["a", "b"])
    standing = await resolve_standing(enforcer, "u1", version=0, verified_claims={})
    assert standing == Standing(
        principal="u1",
        policy=standing.policy,
        owner=None,
        owner_policy=None,
        effective_scopes=["a", "b"],
        is_admin=False,
    )
    assert standing.policy.scopes == ["a", "b"]


async def test_an_owned_key_is_capped_by_its_stored_owner(enforcer, pg):
    pg.add_policy("k1", scopes=["a", "b"], policy_data={OWNER_USER_ID_CLAIM: "o1"})
    pg.add_policy("o1", scopes=["b", "c"])
    standing = await resolve_standing(enforcer, "k1", version=0, verified_claims={OWNER_USER_ID_CLAIM: "o1"})
    assert standing.owner == "o1"
    assert standing.owner_policy is not None
    assert standing.owner_policy.scopes == ["b", "c"]
    assert standing.effective_scopes == ["b"]
    assert standing.is_admin is False


async def test_an_admin_owners_condition_free_key_is_admin(enforcer, pg):
    pg.add_policy("k1", scopes=["*"], policy_data={OWNER_USER_ID_CLAIM: "o1"})
    pg.add_policy("o1", scopes=["*"])
    standing = await resolve_standing(enforcer, "k1", version=0, verified_claims=None)
    assert standing.is_admin is True


async def test_a_disabled_owner_denies_naming_the_owner(enforcer, pg):
    pg.add_policy("k1", scopes=["a"], policy_data={OWNER_USER_ID_CLAIM: "o1"})
    pg.add_policy("o1", scopes=["a"], policy_data={"disabled": True})
    denied = await _denied(enforcer, "k1", verified_claims=None)
    assert (denied.reason, denied.principal, denied.subject) == (StandingDenyReason.OWNER_DISABLED, "k1", "o1")


async def test_an_owner_with_no_policy_denies_naming_the_owner(enforcer, pg):
    pg.add_policy("k1", scopes=["a"], policy_data={OWNER_USER_ID_CLAIM: "o1"})
    denied = await _denied(enforcer, "k1", verified_claims=None)
    assert (denied.reason, denied.subject) == (StandingDenyReason.OWNER_NO_POLICY, "o1")


@pytest.mark.parametrize(
    ("stored", "claimed", "stands"),
    [
        (None, None, True),
        ("o1", "o1", True),
        ("o1", None, False),
        (None, "o1", False),
        ("o1", "o2", False),
    ],
    ids=["both-absent", "equal", "claim-missing", "stored-missing", "different"],
)
async def test_the_verified_owner_must_equal_the_stored_owner(enforcer, pg, caplog, stored, claimed, stands):
    pg.add_policy("k1", scopes=["a"], policy_data={OWNER_USER_ID_CLAIM: stored} if stored else {})
    pg.add_policy("o1", scopes=["a"])
    pg.add_policy("o2", scopes=["a"])
    claims = {OWNER_USER_ID_CLAIM: claimed} if claimed else {}
    if stands:
        standing = await resolve_standing(enforcer, "k1", version=0, verified_claims=claims)
        assert standing.owner == stored
        return
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.access_control.standing"):
        denied = await _denied(enforcer, "k1", verified_claims=claims)
    assert (denied.reason, denied.subject) == (StandingDenyReason.OWNER_MISMATCH, "k1")
    assert f"stored owner {stored!r}, verified credential owner {claimed!r}" in caplog.text


async def test_no_verified_claims_skip_the_owner_comparison(enforcer, pg):
    pg.add_policy("k1", scopes=["a"], policy_data={OWNER_USER_ID_CLAIM: "o1"})
    pg.add_policy("o1", scopes=["a"])
    standing = await resolve_standing(enforcer, "k1", version=0, verified_claims=None)
    assert standing.owner == "o1"


@pytest.mark.parametrize(
    ("stored", "bound", "stands"),
    [
        ("fp1", "fp1", True),
        (None, "", True),
        ("fp1", "fp2", False),
        ("fp1", "", False),
        (None, "fp1", False),
    ],
    ids=["same-mint", "fingerprint-less-account", "remint", "minted-vs-empty", "lost-fingerprint"],
)
async def test_a_bound_fingerprint_must_still_be_the_live_one(enforcer, pg, stored, bound, stands):
    pg.add_policy("k1", scopes=["a"], policy_data={KEY_FINGERPRINT_CLAIM: stored} if stored else {})
    if stands:
        await resolve_standing(enforcer, "k1", version=0, verified_claims=None, bound_fingerprint=bound)
        return
    denied = await _denied(enforcer, "k1", verified_claims=None, bound_fingerprint=bound)
    assert denied.reason is StandingDenyReason.FINGERPRINT_MISMATCH


async def test_the_order_is_policy_then_disabled_then_fingerprint_then_owner(enforcer, pg):
    # Disabled AND a mismatched fingerprint AND a mismatched owner: disabled is read first.
    pg.add_policy(
        "k1",
        scopes=["a"],
        policy_data={"disabled": True, KEY_FINGERPRINT_CLAIM: "fp1", OWNER_USER_ID_CLAIM: "o1"},
    )
    denied = await _denied(enforcer, "k1", verified_claims={}, bound_fingerprint="fp2")
    assert denied.reason is StandingDenyReason.DISABLED

    # A mismatched fingerprint AND a mismatched owner: the fingerprint is read first.
    pg.add_policy("k2", scopes=["a"], policy_data={KEY_FINGERPRINT_CLAIM: "fp1", OWNER_USER_ID_CLAIM: "o1"})
    denied = await _denied(enforcer, "k2", verified_claims={}, bound_fingerprint="fp2")
    assert denied.reason is StandingDenyReason.FINGERPRINT_MISMATCH

    # A mismatched owner AND a disabled owner: the mismatch is read before the owner's row.
    pg.add_policy("k3", scopes=["a"], policy_data={OWNER_USER_ID_CLAIM: "o3"})
    pg.add_policy("o3", scopes=["a"], policy_data={"disabled": True})
    denied = await _denied(enforcer, "k3", verified_claims={})
    assert denied.reason is StandingDenyReason.OWNER_MISMATCH


async def test_store_faults_propagate_as_themselves(monkeypatch, enforcer, pg):
    pg.fault = ("SELECT", RuntimeError("store down"))
    with pytest.raises(RuntimeError, match="store down"):
        await resolve_standing(enforcer, "k1", version=0, verified_claims=None)


# -- jq_passes ---------------------------------------------------------------


def _condition(text: str) -> dict:
    return {"content": text}


async def test_a_principal_without_an_owner_condition_runs_one_pass(enforcer, pg):
    pg.add_policy("k1", scopes=["a"], policy_data={OWNER_USER_ID_CLAIM: "o1"}, condition=_condition("true"))
    pg.add_policy("o1", scopes=["a"])
    standing = await resolve_standing(enforcer, "k1", version=0, verified_claims=None)
    passes = jq_passes(standing, user_id="k1", claims={"c": 1}, live_context={"x": 2}, scopes=["a"], now=7.0)
    assert [p.principal for p in passes] == ["k1"]
    assert passes[0].condition == TemplatedText(content="true")
    assert passes[0].context_for("GET", "/api/x") == {
        "sub": "k1",
        "scopes": ["a"],
        "identity": {"c": 1},
        "policy": {OWNER_USER_ID_CLAIM: "o1"},
        "context": {"x": 2},
        "request": {"method": "GET", "path": "/api/x"},
        "system": {"time": 7.0},
    }


async def test_an_owner_condition_adds_a_pass_over_the_owners_policy(enforcer, pg):
    pg.add_policy("k1", scopes=["a", "b"], policy_data={OWNER_USER_ID_CLAIM: "o1"})
    pg.add_policy("o1", scopes=["a", "c"], policy_data={"tier": 2}, condition=_condition(".policy.tier == 2"))
    standing = await resolve_standing(enforcer, "k1", version=0, verified_claims=None)
    passes = jq_passes(standing, user_id="k1", claims={}, live_context={}, scopes=standing.effective_scopes, now=1.0)
    assert [p.principal for p in passes] == ["k1", "o1"]
    key_context = passes[0].context_for("POST", "/p")
    owner_context = passes[1].context_for("POST", "/p")
    assert key_context["scopes"] == ["a"]
    assert key_context["policy"] == {OWNER_USER_ID_CLAIM: "o1"}
    assert owner_context["sub"] == "k1"
    assert owner_context["scopes"] == ["a", "c"]
    assert owner_context["policy"] == {"tier": 2}
    assert passes[1].condition == TemplatedText(content=".policy.tier == 2")
