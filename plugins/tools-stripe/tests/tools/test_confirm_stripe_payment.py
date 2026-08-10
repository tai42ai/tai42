"""Tests for the ``confirm_stripe_payment`` bridge: the whitelisted answer shape, the livemode
assert across all four key prefixes, the validation raises, the empty-env cases, and the uncaught
door verdict."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe._internal.tools.stripe_client import CallbackDoorError
from tai42_tools_stripe.tools.confirm_stripe_payment import confirm_stripe_payment

_WHITELIST = {"status", "session_id", "payment_intent", "amount_total", "currency", "customer_email"}


def _session(callback_url: str, **overrides: Any) -> dict[str, Any]:
    session = {
        "id": "cs_1",
        "payment_intent": "pi_1",
        "payment_status": "paid",
        "amount_total": 5000,
        "currency": "usd",
        "livemode": False,
        "customer_email": "buyer@example.com",
        "metadata": {
            "tai_callback_url": callback_url,
            "tai_amount": "5000",
            "tai_currency": "usd",
        },
    }
    session.update(overrides)
    return session


def _event(session: dict[str, Any]) -> dict[str, Any]:
    return {"type": "checkout.session.completed", "id": "evt_1", "session": session}


def _callback(base: str) -> str:
    return f"{base}/api/interactions/callback/tkt"


@pytest.mark.usefixtures("curl_app")
def test_happy_path_posts_whitelisted_payload(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", public_base=stub_server.base_url, bridge_secret="bridge-secret")
    session = _session(_callback(stub_server.base_url))
    result = asyncio.run(confirm_stripe_payment(_event(session)))

    assert result == {"data": {"status": "answered"}}
    request = stub_server.requests[0]
    assert request["headers"]["x-tai-bridge-secret"] == "bridge-secret"
    posted = json.loads(request["body"])
    assert set(posted) == _WHITELIST
    assert "metadata" not in posted
    assert posted["status"] == "paid"
    assert posted["session_id"] == "cs_1"
    assert posted["payment_intent"] == "pi_1"
    assert posted["amount_total"] == 5000
    assert posted["customer_email"] == "buyer@example.com"


@pytest.mark.usefixtures("curl_app")
def test_already_answered_is_success(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", public_base=stub_server.base_url, bridge_secret="bridge-secret")
    stub_server.set_responder(lambda request: (200, {}, '{"data": {"status": "already_answered"}}'))
    result = asyncio.run(confirm_stripe_payment(_event(_session(_callback(stub_server.base_url)))))
    assert result == {"data": {"status": "already_answered"}}


@pytest.mark.parametrize(
    ("event", "match"),
    [
        ({"type": "checkout.session.expired", "session": {}}, "checkout.session.completed"),
        ({"type": "checkout.session.completed", "session": None}, "session"),
        ({"type": "checkout.session.completed"}, "session"),
    ],
)
def test_event_shape_validation_raises(event: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        asyncio.run(confirm_stripe_payment(event))


@pytest.mark.parametrize("payment_status", ["unpaid", "no_payment_required"])
def test_non_paid_status_raises(payment_status: str) -> None:
    session = _session("https://acme.example/cb", payment_status=payment_status)
    with pytest.raises(ValueError, match="payment_status"):
        asyncio.run(confirm_stripe_payment(_event(session)))


def test_missing_callback_url_raises(stripe_env: Callable[..., None]) -> None:
    stripe_env(secret_key="sk_test_abc")
    session = _session("https://acme.example/cb")
    session["metadata"]["tai_callback_url"] = ""
    with pytest.raises(ValueError, match="tai_callback_url"):
        asyncio.run(confirm_stripe_payment(_event(session)))


def test_amount_stamp_mismatch_raises(stripe_env: Callable[..., None]) -> None:
    stripe_env(secret_key="sk_test_abc")
    session = _session("https://acme.example/cb")
    session["metadata"]["tai_amount"] = "4000"
    with pytest.raises(ValueError, match="amount_total"):
        asyncio.run(confirm_stripe_payment(_event(session)))


def test_currency_stamp_mismatch_raises(stripe_env: Callable[..., None]) -> None:
    stripe_env(secret_key="sk_test_abc")
    session = _session("https://acme.example/cb")
    session["metadata"]["tai_currency"] = "eur"
    with pytest.raises(ValueError, match="currency"):
        asyncio.run(confirm_stripe_payment(_event(session)))


@pytest.mark.parametrize(
    ("key", "livemode"),
    [
        ("sk_test_abc", True),
        ("rk_test_abc", True),
        ("sk_live_abc", False),
        ("rk_live_abc", False),
    ],
)
def test_livemode_mismatch_raises(stripe_env: Callable[..., None], key: str, livemode: bool) -> None:
    from tai42_tools_stripe._internal.tools.stripe_client import StripeLivemodeMismatch

    stripe_env(secret_key=key)
    session = _session("https://acme.example/cb", livemode=livemode)
    with pytest.raises(StripeLivemodeMismatch):
        asyncio.run(confirm_stripe_payment(_event(session)))


@pytest.mark.usefixtures("curl_app")
@pytest.mark.parametrize(
    ("key", "livemode"),
    [
        ("sk_test_abc", False),
        ("rk_test_abc", False),
        ("sk_live_abc", True),
        ("rk_live_abc", True),
    ],
)
def test_matching_livemode_answers_normally(
    stripe_env: Callable[..., None], stub_server: Any, key: str, livemode: bool
) -> None:
    stripe_env(secret_key=key, public_base=stub_server.base_url, bridge_secret="bridge-secret")
    session = _session(_callback(stub_server.base_url), livemode=livemode)
    result = asyncio.run(confirm_stripe_payment(_event(session)))
    assert result == {"data": {"status": "answered"}}


def test_empty_secret_key_names_the_var_on_the_no_stripe_call_path(
    stripe_env: Callable[..., None],
) -> None:
    # This tool issues no Stripe request, so its only read of the key is the livemode assert -- the
    # single _secret_key() funnel must still name STRIPE_SECRET_KEY, never complain about ''.
    stripe_env(secret_key="")
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY"):
        asyncio.run(confirm_stripe_payment(_event(_session("https://acme.example/cb"))))


@pytest.mark.usefixtures("curl_app")
def test_empty_bridge_secret_names_the_var(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", public_base=stub_server.base_url, bridge_secret="")
    with pytest.raises(ValueError, match="TAI_BRIDGE_CALLBACK_SECRET"):
        asyncio.run(confirm_stripe_payment(_event(_session(_callback(stub_server.base_url)))))


@pytest.mark.usefixtures("curl_app")
def test_empty_public_base_names_the_var(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", public_base="", bridge_secret="bridge-secret")
    with pytest.raises(ValueError, match="INTERACTIONS_PUBLIC_BASE_URL"):
        asyncio.run(confirm_stripe_payment(_event(_session(_callback(stub_server.base_url)))))


@pytest.mark.usefixtures("curl_app")
@pytest.mark.parametrize("status", [404, 400])
def test_door_verdict_propagates_uncaught(stripe_env: Callable[..., None], stub_server: Any, status: int) -> None:
    stripe_env(secret_key="sk_test_abc", public_base=stub_server.base_url, bridge_secret="bridge-secret")
    stub_server.set_responder(lambda request: (status, {}, "no"))
    with pytest.raises(CallbackDoorError) as excinfo:
        asyncio.run(confirm_stripe_payment(_event(_session(_callback(stub_server.base_url)))))
    assert excinfo.value.status == status


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.confirm_stripe_payment")
    assert set(app.tools.registered) == {"confirm_stripe_payment"}
