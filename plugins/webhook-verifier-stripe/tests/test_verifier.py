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
import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from tai42_contract.webhooks import SeenSetClaim, WebhookVerificationError

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


def _event_body(event_id: str, data_object_id: str = "obj_generic_1") -> bytes:
    """A realistic Stripe event envelope with a top-level ``id`` and a nested object.

    Content is generic on purpose: the top-level ``id`` is the idempotency key the
    seen-set keys on, and the nested ``data.object.id`` is deliberately DISTINCT to
    prove it is not the key.
    """
    event = {
        "id": event_id,
        "object": "event",
        "api_version": "2020-08-27",
        "created": _EXAMPLE_TS,
        "type": "ping",
        "data": {"object": {"id": data_object_id, "object": "thing"}},
    }
    return json.dumps(event).encode("utf-8")


def _replay_defense(body: bytes, config: dict[str, Any], *, timestamp: int = _EXAMPLE_TS) -> Any:
    headers = {"Stripe-Signature": _header(timestamp, _EXAMPLE_V1)}
    return StripeWebhookVerifier().replay_defense(body, headers, config)


def test_replay_defense_is_seen_set_keyed_on_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A captured, validly-signed delivery replayed within the freshness window would
    pass ``verify`` again, so replay_defense is a SeenSetClaim keyed on the Stripe
    event id, with a TTL running to the signed-timestamp accept-window end."""
    # Signed timestamp == now, so ``t + tolerance - now`` == tolerance (300).
    _freeze(monkeypatch, _EXAMPLE_TS)
    body = _event_body("evt_generic_alpha")
    defense = _replay_defense(body, _config())
    assert isinstance(defense, SeenSetClaim)
    assert defense.key == "stripe:evt_generic_alpha"
    assert defense.ttl_seconds == 300


def test_replay_defense_replayed_delivery_yields_same_key() -> None:
    """The identical delivery replayed yields the SAME claim key — the seen-set can dedup it."""
    body = _event_body("evt_generic_alpha")
    first = _replay_defense(body, _config())
    second = _replay_defense(body, _config())
    assert isinstance(first, SeenSetClaim)
    assert isinstance(second, SeenSetClaim)
    assert first.key == second.key


def test_replay_defense_distinct_events_yield_distinct_keys() -> None:
    """Two distinct events (distinct top-level ids) yield DISTINCT keys even when their
    ``data`` is identical — the key tracks the event id, not the body content."""
    body_one = _event_body("evt_generic_alpha", data_object_id="obj_shared")
    body_two = _event_body("evt_generic_beta", data_object_id="obj_shared")
    first = _replay_defense(body_one, _config())
    second = _replay_defense(body_two, _config())
    assert isinstance(first, SeenSetClaim)
    assert isinstance(second, SeenSetClaim)
    assert first.key == "stripe:evt_generic_alpha"
    assert second.key == "stripe:evt_generic_beta"
    assert first.key != second.key


def test_replay_defense_custom_tolerance_flows_into_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom ``tolerance_seconds`` widens the claim's ttl_seconds so the seen-set
    remembers the event at least as long as ``verify`` would accept a replay."""
    _freeze(monkeypatch, _EXAMPLE_TS)
    body = _event_body("evt_generic_alpha")
    defense = _replay_defense(body, _config(tolerance_seconds=900))
    assert isinstance(defense, SeenSetClaim)
    assert defense.ttl_seconds == 900


def test_replay_defense_future_timestamp_ttl_exceeds_plain_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future-dated ``t`` (sender clock ahead) yields a TTL of ``t + tolerance - now``,
    which EXCEEDS the plain tolerance — so the seen-set does not forget while ``verify``
    would still accept the replay. A flat receipt-anchored TTL would forget early here."""
    now = _EXAMPLE_TS
    future_t = _EXAMPLE_TS + 120
    _freeze(monkeypatch, now)
    defense = _replay_defense(_event_body("evt_future"), _config(), timestamp=future_t)
    assert isinstance(defense, SeenSetClaim)
    assert defense.ttl_seconds == 120 + 300
    assert defense.ttl_seconds > 300


def test_replay_defense_past_timestamp_ttl_below_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A past ``t`` (already partway through the accept window) yields a TTL below the
    plain tolerance — the seen-set forgets no earlier than when ``verify`` starts stale-rejecting."""
    now = _EXAMPLE_TS
    past_t = _EXAMPLE_TS - 100
    _freeze(monkeypatch, now)
    defense = _replay_defense(_event_body("evt_past"), _config(), timestamp=past_t)
    assert isinstance(defense, SeenSetClaim)
    assert defense.ttl_seconds == 300 - 100
    assert defense.ttl_seconds < 300


def test_replay_defense_ttl_rounds_up_on_fractional_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL rounds UP (fail-safe): with a fractional ``t + tolerance - now`` the seen-set must
    not forget before ``verify`` stops accepting, so a truncating ``int`` would reopen a sub-second
    replay. Here ``t + tolerance - now`` is 299.6, which must round to 300, not 299."""
    _freeze(monkeypatch, _EXAMPLE_TS + 0.4)
    defense = _replay_defense(_event_body("evt_fractional"), _config(), timestamp=_EXAMPLE_TS)
    assert isinstance(defense, SeenSetClaim)
    assert defense.ttl_seconds == 300  # ceil(299.6); a truncating int() would give 299


def test_replay_defense_missing_signature_header_fails_closed() -> None:
    """The signed ``t`` anchors the TTL, so a delivery reaching replay_defense with no
    ``Stripe-Signature`` header cannot be anchored — it fails CLOSED, as verify does."""
    body = _event_body("evt_generic_alpha")
    with pytest.raises(WebhookVerificationError, match="missing Stripe-Signature"):
        StripeWebhookVerifier().replay_defense(body, {}, _config())


def test_replay_defense_unparseable_signature_header_fails_closed() -> None:
    """An unparseable ``Stripe-Signature`` (no ``t=`` element) cannot yield the signed
    timestamp the TTL anchors on, so it fails CLOSED."""
    body = _event_body("evt_generic_alpha")
    with pytest.raises(WebhookVerificationError, match="exactly one t="):
        StripeWebhookVerifier().replay_defense(body, {"Stripe-Signature": "garbage"}, _config())


def test_replay_defense_no_top_level_id_fails_closed() -> None:
    """A body with no top-level ``id`` cannot be deduped, so it fails CLOSED."""
    body = json.dumps({"object": "event", "type": "ping"}).encode("utf-8")
    with pytest.raises(WebhookVerificationError, match="top-level 'id'"):
        _replay_defense(body, _config())


def test_replay_defense_empty_id_fails_closed() -> None:
    """An empty top-level ``id`` cannot key a seen-set, so it fails CLOSED."""
    body = json.dumps({"id": "", "object": "event", "type": "ping"}).encode("utf-8")
    with pytest.raises(WebhookVerificationError, match="top-level 'id'"):
        _replay_defense(body, _config())


def test_replay_defense_non_string_id_fails_closed() -> None:
    """A non-string top-level ``id`` cannot key a seen-set, so it fails CLOSED."""
    body = json.dumps({"id": 123, "object": "event", "type": "ping"}).encode("utf-8")
    with pytest.raises(WebhookVerificationError, match="top-level 'id'"):
        _replay_defense(body, _config())


@pytest.mark.parametrize("body", [b"[1,2]", b"123", b'"str"'])
def test_replay_defense_non_dict_json_fails_closed(body: bytes) -> None:
    """A valid-JSON body that is not an object (a list/number/string) has no top-level
    ``id`` mapping, so it cannot be deduped and fails CLOSED."""
    with pytest.raises(WebhookVerificationError, match="top-level 'id'"):
        _replay_defense(body, _config())


def test_replay_defense_invalid_json_fails_closed() -> None:
    """A body that is not valid JSON cannot be parsed for its id, so it fails CLOSED."""
    with pytest.raises(WebhookVerificationError, match="not valid JSON"):
        _replay_defense(b"{not json", _config())


def test_replay_defense_invalid_utf8_body_fails_closed() -> None:
    """A body that is not valid UTF-8 cannot be JSON-decoded for its id, so it fails CLOSED."""
    with pytest.raises(WebhookVerificationError, match="not valid JSON"):
        _replay_defense(b"\xff", _config())


def test_is_post_only() -> None:
    """A body-signature verifier is POST-only: a door rejects GET for its topic."""
    assert StripeWebhookVerifier().post_only is True


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    """Importing the package registers the ``stripe`` verifier on the handle."""
    app = load_registrations("tai42_webhook_verifier_stripe")
    assert set(app.webhook_verifiers.registered) == {"stripe"}
    assert isinstance(app.webhook_verifiers.get("stripe"), StripeWebhookVerifier)
