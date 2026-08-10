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

    from tai42_contract.webhooks import WebhookVerifier

    class _Ok:
        async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
            return None

    class _Missing:
        pass

    assert isinstance(_Ok(), WebhookVerifier)
    assert not isinstance(_Missing(), WebhookVerifier)


def test_verify_is_a_coroutine_signature():
    from tai42_contract.webhooks import WebhookVerifier

    sig = inspect.signature(WebhookVerifier.verify)
    assert list(sig.parameters) == ["self", "body", "headers", "config"]
