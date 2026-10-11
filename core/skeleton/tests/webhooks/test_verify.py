"""The one webhook verification both verifying doors run, proven with a neutral synthetic verifier."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.hooks import TopicVerifierBinding
from tai42_contract.webhooks import SeenSetClaim, WebhookVerificationError

from tai42_skeleton.webhooks.verify import (
    POST_ONLY,
    VERIFY_ERROR,
    VERIFY_FAILED,
    WebhookVerifyFailure,
    verify_webhook,
)


class _ProbeVerifier:
    """A neutral verifier: accepts a body equal to ``config['expect']``, else raises the configured fault."""

    def __init__(self, *, post_only: bool = False, bug: bool = False) -> None:
        self.post_only = post_only
        self._bug = bug
        self.replay_calls = 0

    async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
        if self._bug:
            raise RuntimeError("verifier bug")
        if body != config["expect"].encode():
            raise WebhookVerificationError("body mismatch")

    def replay_defense(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> SeenSetClaim:
        self.replay_calls += 1
        return SeenSetClaim(key=headers["x-id"], ttl_seconds=60)


class _Registry:
    def __init__(self, verifiers: dict[str, Any]) -> None:
        self._verifiers = verifiers

    def get(self, name: str) -> Any:
        return self._verifiers[name]


def _bound(**verifiers: Any) -> Any:
    return tai42_app.bound(SimpleNamespace(webhook_verifiers=_Registry(verifiers)))


_BINDING = TopicVerifierBinding(verifier="probe", config={"expect": "signed"})


async def _verify(raw: bytes, *, method: str = "POST", with_replay_defense: bool = False) -> Any:
    return await verify_webhook(
        _BINDING, raw, {"x-id": "d-1"}, method=method, subject="topic probe", with_replay_defense=with_replay_defense
    )


async def test_a_passing_verification_returns_the_verdict() -> None:
    verifier = _ProbeVerifier(post_only=True)
    with _bound(probe=verifier):
        verdict = await _verify(b"signed")
    assert verdict.post_only is True
    assert verdict.replay_defense is None
    assert verifier.replay_calls == 0


async def test_the_replay_defense_is_computed_only_when_asked() -> None:
    verifier = _ProbeVerifier()
    with _bound(probe=verifier):
        verdict = await _verify(b"signed", with_replay_defense=True)
    assert verdict.replay_defense == SeenSetClaim(key="d-1", ttl_seconds=60)
    assert verifier.replay_calls == 1


@pytest.mark.parametrize(
    ("verifiers", "raw", "method", "status", "message", "kind"),
    [
        ({"probe": _ProbeVerifier()}, b"forged", "POST", 401, VERIFY_FAILED, ErrorKind.UNAUTHORIZED),
        ({"probe": _ProbeVerifier(post_only=True)}, b"signed", "GET", 405, POST_ONLY, ErrorKind.BAD_INPUT),
        ({}, b"signed", "POST", 500, VERIFY_ERROR, ErrorKind.UNKNOWN),
        ({"probe": _ProbeVerifier(bug=True)}, b"signed", "POST", 500, VERIFY_ERROR, ErrorKind.UNKNOWN),
    ],
    ids=["signature-failure", "get-on-post-only", "unknown-verifier", "verifier-bug"],
)
async def test_every_failure_raises_its_status_and_constant_message(
    verifiers: dict[str, Any], raw: bytes, method: str, status: int, message: str, kind: ErrorKind
) -> None:
    with _bound(**verifiers), pytest.raises(WebhookVerifyFailure) as caught:
        await _verify(raw, method=method)
    assert caught.value.status == status
    assert caught.value.message == message
    assert error_kind(caught.value) is kind


async def test_a_get_on_a_verifier_that_signs_more_than_the_body_is_verified() -> None:
    with _bound(probe=_ProbeVerifier(post_only=False)):
        verdict = await _verify(b"signed", method="GET")
    assert verdict.post_only is False


def test_the_constant_bodies() -> None:
    assert VERIFY_FAILED == "webhook verification failed"
    assert VERIFY_ERROR == "webhook verification error"
    assert POST_ONLY == "this verified topic accepts POST delivery only"
