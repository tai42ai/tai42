"""Tests for the ``create_stripe_checkout`` tool: the form-encoded create body, the metadata
stamps, the deterministic Idempotency-Key, argument validation, and the empty/missing secret."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import pytest

from tai42_tools_stripe._internal.tools.stripe_client import STRIPE_API_VERSION
from tai42_tools_stripe.tools.create_stripe_checkout import create_stripe_checkout

_URL = "https://checkout.stripe.example/pay/cs_1"


def _ok(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
    return 200, {"Content-Type": "application/json"}, f'{{"id": "cs_1", "url": "{_URL}"}}'


def _call(callback_url: str = "https://acme.example/cb") -> str:
    return asyncio.run(
        create_stripe_checkout(
            callback_url=callback_url,
            amount=5000,
            currency="usd",
            product_name="Pro",
            success_url="https://acme.example/thanks",
        )
    )


@pytest.mark.usefixtures("curl_app")
def test_happy_path_posts_form_encoded_body(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_ok)

    result = _call()
    assert result == _URL

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/checkout/sessions"
    body = parse_qs(request["body"])
    assert body["mode"] == ["payment"]
    assert body["line_items[0][quantity]"] == ["1"]
    assert body["line_items[0][price_data][unit_amount]"] == ["5000"]
    assert body["line_items[0][price_data][currency]"] == ["usd"]
    assert body["line_items[0][price_data][product_data][name]"] == ["Pro"]
    assert body["success_url"] == ["https://acme.example/thanks"]
    assert body["metadata[tai_callback_url]"] == ["https://acme.example/cb"]
    assert body["metadata[tai_amount]"] == ["5000"]
    assert body["metadata[tai_currency]"] == ["usd"]
    assert "cancel_url" not in body

    headers = request["headers"]
    assert headers["authorization"] == "Bearer sk_test_abc"
    assert headers["stripe-version"] == STRIPE_API_VERSION
    assert headers["idempotency-key"].startswith("tai-")


@pytest.mark.usefixtures("curl_app")
def test_cancel_url_is_forwarded_when_supplied(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_ok)
    asyncio.run(
        create_stripe_checkout(
            callback_url="https://acme.example/cb",
            amount=5000,
            currency="usd",
            product_name="Pro",
            success_url="https://acme.example/thanks",
            cancel_url="https://acme.example/cancelled",
        )
    )
    body = parse_qs(stub_server.requests[0]["body"])
    assert body["cancel_url"] == ["https://acme.example/cancelled"]


@pytest.mark.usefixtures("curl_app")
def test_idempotency_key_is_deterministic(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_ok)

    _call("https://acme.example/cb-one")
    _call("https://acme.example/cb-one")
    _call("https://acme.example/cb-two")

    keys = [r["headers"]["idempotency-key"] for r in stub_server.requests]
    assert keys[0] == keys[1]  # same callback URL -> same key
    assert keys[0] != keys[2]  # different callback URL -> different key


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"amount": 0}, "amount"),
        ({"amount": -3}, "amount"),
        ({"currency": "USD"}, "currency"),
        ({"currency": "us"}, "currency"),
        ({"currency": "us1"}, "currency"),
        ({"success_url": ""}, "success_url"),
    ],
)
def test_argument_validation_raises(kwargs: dict[str, Any], match: str) -> None:
    base = {
        "callback_url": "https://acme.example/cb",
        "amount": 5000,
        "currency": "usd",
        "product_name": "Pro",
        "success_url": "https://acme.example/thanks",
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        asyncio.run(create_stripe_checkout(**base))


@pytest.mark.parametrize("secret_key", [None, ""])
def test_missing_or_empty_secret_key_raises(stripe_env: Callable[..., None], secret_key: str | None) -> None:
    stripe_env(secret_key=secret_key)
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.create_stripe_checkout")
    assert set(app.tools.registered) == {"create_stripe_checkout"}
