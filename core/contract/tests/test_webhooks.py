"""Tests for the webhook-verifier contract types.

The ``AppWebhookVerifiers`` protocol membership is covered by the frozen-facade
partition test; here we pin the typed error and the verifier Protocol shape the
facet exposes.
"""

from __future__ import annotations

import inspect

import pytest


def test_verification_error_is_a_distinct_exception_type():
    from tai42_contract.webhooks import WebhookVerificationError

    assert issubclass(WebhookVerificationError, Exception)
    # A distinct type so a door can catch verification failure without also
    # swallowing unrelated errors.
    assert WebhookVerificationError is not Exception
    with pytest.raises(WebhookVerificationError):
        raise WebhookVerificationError("bad signature")


def test_verifier_protocol_is_runtime_checkable_and_shaped():
    from collections.abc import Mapping
    from typing import Any

    from tai42_contract.webhooks import FreshnessWindow, ReplayDefense, WebhookVerifier

    class _Ok:
        async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
            return None

        def replay_defense(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> ReplayDefense:
            return FreshnessWindow()

    class _VerifyOnly:
        async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
            return None

    class _Missing:
        pass

    assert isinstance(_Ok(), WebhookVerifier)
    # replay_defense is a REQUIRED protocol member: a verifier that only verifies no
    # longer satisfies the contract, so a scheme cannot ship replay defense forgotten.
    assert not isinstance(_VerifyOnly(), WebhookVerifier)
    assert not isinstance(_Missing(), WebhookVerifier)


def test_verify_is_a_coroutine_signature():
    from tai42_contract.webhooks import WebhookVerifier

    sig = inspect.signature(WebhookVerifier.verify)
    assert list(sig.parameters) == ["self", "body", "headers", "config"]


def test_replay_defense_is_part_of_the_protocol():
    from tai42_contract.webhooks import WebhookVerifier

    sig = inspect.signature(WebhookVerifier.replay_defense)
    assert list(sig.parameters) == ["self", "body", "headers", "config"]


def test_seen_set_claim_holds_key_and_positive_ttl():
    from tai42_contract.webhooks import ReplayDefense, SeenSetClaim

    claim = SeenSetClaim(key="github:abc", ttl_seconds=300)
    assert isinstance(claim, ReplayDefense)
    assert (claim.key, claim.ttl_seconds) == ("github:abc", 300)


@pytest.mark.parametrize("ttl", [0, -1])
def test_seen_set_claim_rejects_non_positive_ttl(ttl: int):
    from tai42_contract.webhooks import SeenSetClaim

    # A claim without a positive TTL would leak a permanent key or defend nothing.
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        SeenSetClaim(key="k", ttl_seconds=ttl)


def test_seen_set_claim_rejects_bool_ttl():
    from tai42_contract.webhooks import SeenSetClaim

    # bool is an int subclass but not a sane duration.
    with pytest.raises(ValueError, match="ttl_seconds must be an int"):
        SeenSetClaim(key="k", ttl_seconds=True)


def test_seen_set_claim_rejects_empty_key():
    from tai42_contract.webhooks import SeenSetClaim

    with pytest.raises(ValueError, match="non-empty key"):
        SeenSetClaim(key="", ttl_seconds=300)


def test_freshness_window_is_a_replay_defense():
    from tai42_contract.webhooks import FreshnessWindow, ReplayDefense

    assert isinstance(FreshnessWindow(), ReplayDefense)


def test_replay_defense_base_is_not_bare_instantiable():
    from tai42_contract.webhooks import ReplayDefense

    # ABSTRACT: a bare ReplayDefense declares no defense yet would pass an
    # ``isinstance(..., ReplayDefense)`` gate, so it is refused at construction and
    # the ingress cannot be handed an undefended-but-typed replay declaration.
    with pytest.raises(TypeError, match="ReplayDefense is abstract"):
        ReplayDefense()
