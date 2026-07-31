"""Tests for the GitHub webhook-signature verifier and its registration.

The signature vector is GitHub's documented example, pinned and recomputed in-test.
The secret only ever lives in a ``monkeypatch`` env var, never a committed fixture.
``verify`` is async; each test drives it through ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from tai42_contract.webhooks import WebhookVerificationError

from tai42_webhook_verifier_github.verifier import GitHubWebhookVerifier

# GitHub's documented example vector (Validating deliveries). Example secret only.
_SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
_EXAMPLE_SECRET = "It's a Secret to Everybody"  # GitHub's published example, not a real secret
_EXAMPLE_BODY = b"Hello, World!"
_EXAMPLE_SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def _config() -> dict[str, Any]:
    return {"secret_env": _SECRET_ENV}


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _verify(body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
    return asyncio.run(GitHubWebhookVerifier().verify(body, headers, config))


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the shared secret via env, never a committed value."""
    monkeypatch.setenv(_SECRET_ENV, _EXAMPLE_SECRET)


def test_github_example_vector_is_self_consistent() -> None:
    """The pinned vector matches a fresh recompute — deterministic and stable."""
    assert _sign(_EXAMPLE_BODY, _EXAMPLE_SECRET) == _EXAMPLE_SIGNATURE


@pytest.mark.usefixtures("secret_env")
def test_valid_signature_returns_none() -> None:
    headers = {"X-Hub-Signature-256": _EXAMPLE_SIGNATURE}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env")
def test_header_lookup_is_case_insensitive() -> None:
    headers = {"x-hub-signature-256": _EXAMPLE_SIGNATURE}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env")
def test_tampered_body_fails() -> None:
    headers = {"X-Hub-Signature-256": _EXAMPLE_SIGNATURE}
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY + b"!", headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_missing_header_fails() -> None:
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY, {}, _config())


@pytest.mark.usefixtures("secret_env")
def test_wrong_prefix_fails() -> None:
    # A correct-length hex digest under the wrong (sha1) algorithm prefix.
    digest = hmac.new(_EXAMPLE_SECRET.encode("utf-8"), _EXAMPLE_BODY, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": "sha1=" + digest}
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_truncated_length_fails() -> None:
    # Right prefix, right leading hex, but truncated below 64 chars.
    headers = {"X-Hub-Signature-256": _EXAMPLE_SIGNATURE[:-2]}
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_digest_mismatch_fails() -> None:
    # Well-formed prefix and 64 hex chars, but not the right digest.
    headers = {"X-Hub-Signature-256": "sha256=" + "0" * 64}
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_non_hex_digest_fails() -> None:
    # Right prefix and length, but a non-hex digest — rejected as an ordinary failure.
    headers = {"X-Hub-Signature-256": "sha256=" + "z" * 64}
    with pytest.raises(WebhookVerificationError, match="not valid hex"):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """The final compare goes through ``hmac.compare_digest`` (constant-time)."""
    import tai42_webhook_verifier_github.verifier as verifier_module

    calls: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    # Patch the name the verifier module resolves at call time.
    monkeypatch.setattr(verifier_module.hmac, "compare_digest", spy)

    headers = {"X-Hub-Signature-256": _EXAMPLE_SIGNATURE}
    _verify(_EXAMPLE_BODY, headers, _config())

    assert len(calls) == 1


def test_missing_secret_env_var_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing env var raises loudly (not a WebhookVerificationError) — fails CLOSED."""
    monkeypatch.delenv(_SECRET_ENV, raising=False)
    headers = {"X-Hub-Signature-256": _EXAMPLE_SIGNATURE}
    with pytest.raises(KeyError):
        _verify(_EXAMPLE_BODY, headers, _config())


def test_empty_secret_env_var_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty env var raises loudly — an empty secret keys a forgeable HMAC, so fails CLOSED."""
    monkeypatch.setenv(_SECRET_ENV, "")
    headers = {"X-Hub-Signature-256": _sign(_EXAMPLE_BODY, "")}
    with pytest.raises(ValueError, match="set but empty"):
        _verify(_EXAMPLE_BODY, headers, _config())


def test_is_post_only() -> None:
    """A body-signature verifier is POST-only: a door rejects GET for its topic."""
    assert GitHubWebhookVerifier().post_only is True


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    """Importing the package registers the ``github`` verifier on the handle."""
    app = load_registrations("tai42_webhook_verifier_github")
    assert set(app.webhook_verifiers.registered) == {"github"}
    assert isinstance(app.webhook_verifiers.get("github"), GitHubWebhookVerifier)
