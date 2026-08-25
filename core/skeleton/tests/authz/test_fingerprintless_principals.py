"""The fingerprint-less principal seam: an ACCOUNT user (session-authenticated
human, never minted) carries no per-mint fingerprint, binds with the empty
fingerprint, and matches only a policy that carries none — every other
combination of the ONE equality stays fail-closed."""

import pytest
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM
from tai42_contract.access_control.models import AccessPolicy

from tai42_skeleton.authz import execution as authz_execution
from tai42_skeleton.authz.execution import (
    ExecutionKeyAuthorityError,
    assert_policy_matches_fingerprint,
    rebuild_execution_identity,
)


def _policy(*, fingerprint: str | None = None, scopes: list[str] | None = None) -> AccessPolicy:
    data = {} if fingerprint is None else {KEY_FINGERPRINT_CLAIM: fingerprint}
    return AccessPolicy(scopes=scopes if scopes is not None else ["*"], policy_data=data)


# -- the ONE equality, all four combinations ---------------------------------


def test_a_fingerprintless_principal_matches_the_empty_bound_fingerprint():
    # Account user: no mint identity stored, "" bound -> match (nothing to anchor).
    assert_policy_matches_fingerprint(_policy(), "usr-human", bound_fingerprint="")


def test_a_minted_key_never_matches_the_empty_bound_fingerprint():
    # A gate-off-era record ("" bound) can never bind a MINTED key once the gate is on.
    with pytest.raises(ExecutionKeyAuthorityError):
        assert_policy_matches_fingerprint(_policy(fingerprint="fp-mint"), "svc-key", bound_fingerprint="")


def test_a_bound_fingerprint_never_matches_a_policy_without_one():
    # The key was reminted-away (or the record is foreign): stored None vs a real bound.
    with pytest.raises(ExecutionKeyAuthorityError):
        assert_policy_matches_fingerprint(_policy(), "svc-key", bound_fingerprint="fp-old")


def test_a_minted_key_still_matches_its_own_fingerprint():
    assert_policy_matches_fingerprint(_policy(fingerprint="fp-mint"), "svc-key", bound_fingerprint="fp-mint")


# -- the rebuild builds account principals and stays fail-closed -------------


class _FakeEnforcer:
    def __init__(self, policy: AccessPolicy) -> None:
        self._policy = policy

    async def current_policy_version(self) -> int:
        return 1

    async def get_policy_at(self, user_id: str, version: int) -> AccessPolicy:
        return self._policy


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch):
    class _Settings:
        enable = True

    monkeypatch.setattr(authz_execution, "access_control_settings", lambda: _Settings())

    def wire(policy: AccessPolicy) -> None:
        monkeypatch.setattr(authz_execution, "PolicyEnforcer", lambda settings: _FakeEnforcer(policy))

    return wire


async def test_rebuild_builds_an_account_principal_with_the_empty_fingerprint(gate_on):
    # A session human's policy: scopes but NO mint fingerprint -> a usable identity
    # whose fingerprint is the seam-wide "" spelling (what the ask stamps and the
    # continuation rebind later matches).
    gate_on(_policy(scopes=["*"]))
    identity = await rebuild_execution_identity("usr-human")
    assert identity is not None
    assert identity.user_id == "usr-human"
    assert identity.execution_key_fingerprint == ""


async def test_rebuild_refuses_a_deleted_principal(gate_on):
    # policy_is_empty (no scopes, no condition) -> the authority check refuses ->
    # the rebuild degrades to None, never a substitute principal.
    gate_on(AccessPolicy(scopes=[], policy_data={}))
    assert await rebuild_execution_identity("usr-gone") is None


async def test_rebuild_refuses_a_disabled_principal(gate_on):
    gate_on(AccessPolicy(scopes=["*"], policy_data={"disabled": True}))
    assert await rebuild_execution_identity("usr-off") is None


async def test_rebuild_still_anchors_a_minted_key_to_its_fingerprint(gate_on):
    gate_on(_policy(fingerprint="fp-live", scopes=["*"]))
    identity = await rebuild_execution_identity("svc-key")
    assert identity is not None
    assert identity.execution_key_fingerprint == "fp-live"
