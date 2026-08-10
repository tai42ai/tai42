"""Tests for the Stripe webhook-signature verifier and its registration.

The signature vector is computed locally for the Stripe scheme (``t.body`` keyed by
the signing secret) and pinned, with a self-consistency check asserted first. The
secret only ever lives in a ``monkeypatch`` env var, never a committed fixture. The
clock is frozen by monkeypatching the module's time source — no sleeps. ``verify`` is
async; each test drives it through ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from tai42_contract.webhooks import WebhookVerificationError

import tai42_webhook_verifier_stripe.verifier as verifier_module
from tai42_webhook_verifier_stripe.verifier import StripeWebhookVerifier

_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
_EXAMPLE_SECRET = "whsec_example_test_secret"  # example placeholder, not a real signing secret
_EXAMPLE_BODY = b'{"id":"evt_test","type":"checkout.session.completed"}'
_EXAMPLE_TS = 1700000000
# HMAC-SHA256 of "1700000000." + _EXAMPLE_BODY under _EXAMPLE_SECRET, pinned and
# recomputed in test_stripe_example_vector_is_self_consistent.
_EXAMPLE_V1 = "939faca589b2c73c93fc7133985a5b57afc20ebbb7e756247f39de58baf5bc55"


def _config(**extra: Any) -> dict[str, Any]:
    return {"secret_env": _SECRET_ENV, **extra}


def _sign(body: bytes, secret: str, timestamp: int) -> str:
    signed = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def _header(timestamp: int, *v1s: str) -> str:
    return ",".join([f"t={timestamp}", *[f"v1={v}" for v in v1s]])


def _verify(body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
    return asyncio.run(StripeWebhookVerifier().verify(body, headers, config))


def _freeze(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """Freeze the verifier's clock at ``now`` seconds since the epoch."""
    monkeypatch.setattr(verifier_module.time, "time", lambda: float(now))


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the signing secret via env, never a committed value."""
    monkeypatch.setenv(_SECRET_ENV, _EXAMPLE_SECRET)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze time at the example timestamp so the pinned vector is fresh."""
    _freeze(monkeypatch, _EXAMPLE_TS)


def test_stripe_example_vector_is_self_consistent() -> None:
    """The pinned vector matches a fresh recompute — deterministic and stable."""
    assert _sign(_EXAMPLE_BODY, _EXAMPLE_SECRET, _EXAMPLE_TS) == _EXAMPLE_V1


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_valid_signature_returns_none() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_header_lookup_is_case_insensitive() -> None:
    headers = {"stripe-signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_tampered_body_fails() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY + b"!", headers, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_missing_header_fails() -> None:
    with pytest.raises(WebhookVerificationError):
        _verify(_EXAMPLE_BODY, {}, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_digest_mismatch_fails() -> None:
    # A well-formed 64-hex v1 that is simply the wrong digest.
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, "0" * 64)}
    with pytest.raises(WebhookVerificationError, match=r"no .* matches"):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_multiple_v1_wrong_then_right_passes() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, "0" * 64, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_multiple_v1_right_then_wrong_passes() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1, "0" * 64)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_malformed_wrong_length_plus_valid_passes() -> None:
    # A too-short v1 alongside the correct one: the malformed candidate is skipped.
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, "abcd", _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_malformed_non_hex_64_plus_valid_passes() -> None:
    # A 64-char non-hex v1 alongside the correct one: skipped, not raised on.
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, "z" * 64, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_all_malformed_v1_fails_as_no_match() -> None:
    # No well-formed candidate at all: an ordinary no-match, never a ValueError.
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, "abcd", "z" * 64)}
    with pytest.raises(WebhookVerificationError, match=r"no .* matches"):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_ignored_non_v1_scheme_does_not_break_parsing() -> None:
    # A v0= element (and an unrecognized key) is ignored, not added to candidates.
    header = f"t={_EXAMPLE_TS},v0=deadbeef,scheme=foo,v1={_EXAMPLE_V1}"
    assert _verify(_EXAMPLE_BODY, {"Stripe-Signature": header}, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_element_without_equals_is_ignored() -> None:
    header = f"t={_EXAMPLE_TS},garbage,v1={_EXAMPLE_V1}"
    assert _verify(_EXAMPLE_BODY, {"Stripe-Signature": header}, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_whitespace_around_elements_tolerated() -> None:
    header = f"  t={_EXAMPLE_TS} , v1={_EXAMPLE_V1} "
    assert _verify(_EXAMPLE_BODY, {"Stripe-Signature": header}, _config()) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_no_t_element_fails() -> None:
    headers = {"Stripe-Signature": f"v1={_EXAMPLE_V1}"}
    with pytest.raises(WebhookVerificationError, match="exactly one t="):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_duplicate_t_element_fails() -> None:
    header = f"t={_EXAMPLE_TS},t={_EXAMPLE_TS},v1={_EXAMPLE_V1}"
    with pytest.raises(WebhookVerificationError, match="exactly one t="):
        _verify(_EXAMPLE_BODY, {"Stripe-Signature": header}, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_non_integer_t_fails() -> None:
    header = f"t=not-a-number,v1={_EXAMPLE_V1}"
    with pytest.raises(WebhookVerificationError, match="not an integer"):
        _verify(_EXAMPLE_BODY, {"Stripe-Signature": header}, _config())


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_no_v1_element_fails() -> None:
    headers = {"Stripe-Signature": f"t={_EXAMPLE_TS}"}
    with pytest.raises(WebhookVerificationError, match="no v1"):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_stale_timestamp_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # now is one second past the tolerance window: rejected as a replay.
    _freeze(monkeypatch, _EXAMPLE_TS + 300 + 1)
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(WebhookVerificationError, match="older than the tolerance window"):
        _verify(_EXAMPLE_BODY, headers, _config())


@pytest.mark.usefixtures("secret_env")
def test_fresh_boundary_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    # now is exactly tolerance seconds after t: still fresh (rejection is strictly >).
    _freeze(monkeypatch, _EXAMPLE_TS + 300)
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env")
def test_future_timestamp_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    # now is well BEFORE t (a future-dated delivery from a fast sender clock): the
    # freshness window is one-sided — stale only — so a validly-signed future
    # delivery is accepted, matching Stripe's own libraries. A symmetric-window
    # regression (abs(now - t) > tolerance) would wrongly reject and fail here.
    _freeze(monkeypatch, _EXAMPLE_TS - 600)
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config()) is None


@pytest.mark.usefixtures("secret_env")
def test_custom_tolerance_seconds_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    # A delivery stale under the default 300 verifies under a wider custom window.
    _freeze(monkeypatch, _EXAMPLE_TS + 600)
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    assert _verify(_EXAMPLE_BODY, headers, _config(tolerance_seconds=900)) is None


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_zero_tolerance_raises_value_error() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(ValueError, match="must be positive"):
        _verify(_EXAMPLE_BODY, headers, _config(tolerance_seconds=0))


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_negative_tolerance_raises_value_error() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(ValueError, match="must be positive"):
        _verify(_EXAMPLE_BODY, headers, _config(tolerance_seconds=-1))


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_non_int_tolerance_raises_value_error() -> None:
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(ValueError, match="must be an int"):
        _verify(_EXAMPLE_BODY, headers, _config(tolerance_seconds=1.5))


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_bool_tolerance_raises_value_error() -> None:
    # bool is an int subclass but not a sane duration: True would masquerade as a
    # 1-second window. The verifier special-cases it, so it must be rejected.
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(ValueError, match="must be an int"):
        _verify(_EXAMPLE_BODY, headers, _config(tolerance_seconds=True))


@pytest.mark.usefixtures("secret_env", "frozen_now")
def test_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hmac.compare_digest`` runs for each WELL-FORMED candidate and not a skipped one."""
    calls: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    # Patch the name the verifier module resolves at call time.
    monkeypatch.setattr(verifier_module.hmac, "compare_digest", spy)

    # A skipped malformed candidate, then a wrong-but-well-formed one, then the match.
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, "abcd", "0" * 64, _EXAMPLE_V1)}
    _verify(_EXAMPLE_BODY, headers, _config())

    # Two well-formed candidates compared (the wrong one, then the match); the
    # malformed "abcd" was dropped before the loop and never compared.
    assert len(calls) == 2


def test_missing_secret_env_var_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing env var raises loudly (not a WebhookVerificationError) — fails CLOSED."""
    monkeypatch.delenv(_SECRET_ENV, raising=False)
    _freeze(monkeypatch, _EXAMPLE_TS)
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _EXAMPLE_V1)}
    with pytest.raises(KeyError):
        _verify(_EXAMPLE_BODY, headers, _config())


def test_empty_secret_env_var_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty env var raises loudly — an empty secret keys a forgeable HMAC, so fails CLOSED."""
    monkeypatch.setenv(_SECRET_ENV, "")
    _freeze(monkeypatch, _EXAMPLE_TS)
    headers = {"Stripe-Signature": _header(_EXAMPLE_TS, _sign(_EXAMPLE_BODY, "", _EXAMPLE_TS))}
    with pytest.raises(ValueError, match="set but empty"):
        _verify(_EXAMPLE_BODY, headers, _config())


def test_is_post_only() -> None:
    """A body-signature verifier is POST-only: a door rejects GET for its topic."""
    assert StripeWebhookVerifier().post_only is True


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    """Importing the package registers the ``stripe`` verifier on the handle."""
    app = load_registrations("tai42_webhook_verifier_stripe")
    assert set(app.webhook_verifiers.registered) == {"stripe"}
    assert isinstance(app.webhook_verifiers.get("stripe"), StripeWebhookVerifier)
