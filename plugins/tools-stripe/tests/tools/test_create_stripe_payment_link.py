"""Tests for the ``create_stripe_payment_link`` tool: the form-encoded create body, the caller
metadata stamps, the argument-derived Idempotency-Key, the livemode assert on the created session,
argument and metadata validation, the link/session_id extraction, and the missing/empty secret."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import pytest

from tai42_tools_stripe._internal.tools.stripe_client import StripeLivemodeMismatch
from tai42_tools_stripe.tools.create_stripe_payment_link import create_stripe_payment_link

_URL = "https://checkout.stripe.example/pay/cs_link_1"


def _session(*, livemode: bool = False) -> str:
    return f'{{"id": "cs_link_1", "url": "{_URL}", "livemode": {"true" if livemode else "false"}}}'


def _responder(*, livemode: bool = False) -> Callable[[dict[str, Any]], tuple[int, dict[str, str], str]]:
    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return 200, {"Content-Type": "application/json"}, _session(livemode=livemode)

    return responder


def _call(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "amount": 5000,
        "currency": "usd",
        "product_name": "Sample item",
        "success_url": "https://acme.example/thanks",
        "cancel_url": "https://acme.example/cancelled",
    }
    kwargs.update(overrides)
    return asyncio.run(create_stripe_payment_link(**kwargs))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_returns_link_and_session_id(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = _call()
    assert result == {"link": _URL, "session_id": "cs_link_1"}

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/checkout/sessions"
    body = parse_qs(request["body"])
    assert body["mode"] == ["payment"]
    assert body["line_items[0][quantity]"] == ["1"]
    assert body["line_items[0][price_data][unit_amount]"] == ["5000"]
    assert body["line_items[0][price_data][currency]"] == ["usd"]
    assert body["line_items[0][price_data][product_data][name]"] == ["Sample item"]
    assert body["success_url"] == ["https://acme.example/thanks"]
    assert body["cancel_url"] == ["https://acme.example/cancelled"]
    assert body["metadata[tai_amount]"] == ["5000"]
    assert body["metadata[tai_currency]"] == ["usd"]
    # No callback ask: the ask-only stamp is never sent.
    assert "metadata[tai_callback_url]" not in body

    headers = request["headers"]
    assert headers["authorization"] == "Bearer sk_test_abc"
    assert headers["idempotency-key"].startswith("tai-")


@pytest.mark.usefixtures("curl_app")
def test_caller_metadata_is_stamped_verbatim(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    _call(metadata={"external_ref": "ref-42", "channel_user": "user-9"})
    body = parse_qs(stub_server.requests[0]["body"])
    assert body["metadata[external_ref]"] == ["ref-42"]
    assert body["metadata[channel_user]"] == ["user-9"]
    # The tool's own stamps ride alongside the caller keys.
    assert body["metadata[tai_amount]"] == ["5000"]
    assert body["metadata[tai_currency]"] == ["usd"]


@pytest.mark.usefixtures("curl_app")
def test_caller_metadata_absent_when_not_supplied(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    _call()
    body = parse_qs(stub_server.requests[0]["body"])
    assert body["metadata[tai_amount]"] == ["5000"]
    assert body["metadata[tai_currency]"] == ["usd"]
    # Only the tool's own stamps are present.
    assert [k for k in body if k.startswith("metadata[")] == ["metadata[tai_amount]", "metadata[tai_currency]"]


@pytest.mark.usefixtures("curl_app")
def test_idempotency_key_is_argument_derived(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    _call()
    _call()  # identical arguments -> same session
    _call(amount=6000)  # a different argument -> a different session
    _call(metadata={"external_ref": "ref-x"})  # a metadata discriminator -> a different session

    keys = [r["headers"]["idempotency-key"] for r in stub_server.requests]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]
    assert keys[0] != keys[3]


@pytest.mark.usefixtures("curl_app")
def test_same_metadata_yields_same_key(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    _call(metadata={"external_ref": "ref-1", "channel_user": "user-1"})
    # Key order differs but the sorted dict makes the basis identical.
    _call(metadata={"channel_user": "user-1", "external_ref": "ref-1"})

    keys = [r["headers"]["idempotency-key"] for r in stub_server.requests]
    assert keys[0] == keys[1]


@pytest.mark.usefixtures("curl_app")
def test_livemode_mismatch_on_created_session_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(livemode=True))  # test key, live session -> mismatch
    with pytest.raises(StripeLivemodeMismatch):
        _call()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"amount": 0}, "amount"),
        ({"amount": -3}, "amount"),
        ({"currency": "USD"}, "currency"),
        ({"currency": "us"}, "currency"),
        ({"currency": "us1"}, "currency"),
        ({"product_name": ""}, "product_name"),
        ({"success_url": ""}, "success_url"),
        ({"cancel_url": ""}, "cancel_url"),
        ({"metadata": {"": "v"}}, "non-empty"),
        ({"metadata": {"tai_amount": "9"}}, "reserved"),
        ({"metadata": {"tai_custom": "x"}}, "reserved"),
        ({"metadata": {"external_ref": 7}}, "must be a string"),
    ],
)
def test_argument_validation_raises(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _call(**kwargs)


@pytest.mark.parametrize("secret_key", [None, ""])
def test_missing_or_empty_secret_key_raises(stripe_env: Callable[..., None], secret_key: str | None) -> None:
    stripe_env(secret_key=secret_key)
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.create_stripe_payment_link")
    assert set(app.tools.registered) == {"create_stripe_payment_link"}
