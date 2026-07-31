"""Tests for the pure OAuth2/OIDC crypto helpers."""

from __future__ import annotations

import base64
import hashlib

from tai42_accounts_oidc import oauth

_KEY = b"hmac-signing-secret"


def test_sign_verify_round_trip() -> None:
    state_id = oauth.new_state_id()
    state = oauth.sign_state(state_id, _KEY)
    assert state.startswith(f"{state_id}.")
    assert oauth.verify_state(state, _KEY) == state_id


def test_verify_rejects_tampered_mac() -> None:
    state = oauth.sign_state("abc", _KEY)
    tampered = state[:-1] + ("0" if state[-1] != "0" else "1")
    assert oauth.verify_state(tampered, _KEY) is None


def test_verify_rejects_wrong_key() -> None:
    state = oauth.sign_state("abc", _KEY)
    assert oauth.verify_state(state, b"other-key") is None


def test_verify_rejects_malformed() -> None:
    assert oauth.verify_state("no-dot-here", _KEY) is None
    assert oauth.verify_state(".onlymac", _KEY) is None
    assert oauth.verify_state("onlyid.", _KEY) is None


def test_pkce_pair_is_s256() -> None:
    verifier, challenge = oauth.pkce_pair()
    assert verifier != challenge
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge


def test_generators_are_distinct_nonempty() -> None:
    assert oauth.new_state_id() != oauth.new_state_id()
    assert oauth.new_nonce() != oauth.new_nonce()
    assert oauth.new_sso_code() != oauth.new_sso_code()
    assert all(len(v) > 0 for v in (oauth.new_state_id(), oauth.new_nonce(), oauth.new_sso_code()))
