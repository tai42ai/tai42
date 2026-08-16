"""Tests for the ``create_stripe_webhook_endpoint`` tool: the form-encoded create body, the
returned shape, that the one-time signing secret is returned wrapped in a ``SecretValue`` whose
``reveal()`` is Stripe's verbatim secret, the livemode assert on the created endpoint, argument
validation, and the missing/empty secret."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import pytest
from tai42_contract.secrets import SecretValue

from tai42_tools_stripe._internal.tools.stripe_client import STRIPE_API_VERSION, StripeLivemodeMismatch
from tai42_tools_stripe.tools.create_stripe_webhook_endpoint import create_stripe_webhook_endpoint

_URL = "https://acme.example/hook"
_EVENTS = ["checkout.session.completed"]


def _endpoint_body(
    *, livemode: bool = False, secret: str = "whsec_supersecret", events: list[str] | None = None
) -> str:
    return json.dumps(
        {
            "id": "we_1",
            "object": "webhook_endpoint",
            "url": _URL,
            "enabled_events": events if events is not None else _EVENTS,
            "status": "enabled",
            "secret": secret,
            "livemode": livemode,
        }
    )


def _responder(
    *, code: int = 200, livemode: bool = False, secret: str = "whsec_supersecret", body: str | None = None
) -> Callable[[dict[str, Any]], tuple[int, dict[str, str], str]]:
    payload = body if body is not None else _endpoint_body(livemode=livemode, secret=secret)

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return code, {"Content-Type": "application/json"}, payload

    return responder


def _call(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"url": _URL, "enabled_events": _EVENTS}
    kwargs.update(overrides)
    return asyncio.run(create_stripe_webhook_endpoint(**kwargs))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_returns_shape_and_secret_verbatim(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(secret="whsec_verbatim_123"))

    result = _call()
    assert isinstance(result["secret"], SecretValue)
    assert result["secret"].reveal() == "whsec_verbatim_123"
    assert {k: v for k, v in result.items() if k != "secret"} == {
        "endpoint_id": "we_1",
        "url": _URL,
        "enabled_events": _EVENTS,
        "status": "enabled",
    }
    # The envelope is the fail-safe: an un-audited path that dumps the whole
    # result raises loudly instead of leaking the real secret.
    with pytest.raises(TypeError):
        json.dumps(result)

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/webhook_endpoints"
    body = parse_qs(request["body"])
    assert body["url"] == [_URL]
    assert body["enabled_events[]"] == _EVENTS

    headers = request["headers"]
    assert headers["authorization"] == "Bearer sk_test_abc"
    assert headers["stripe-version"] == STRIPE_API_VERSION


@pytest.mark.usefixtures("curl_app")
def test_multiple_events_are_form_repeated(stripe_env: Callable[..., None], stub_server: Any) -> None:
    events = ["checkout.session.completed", "checkout.session.expired"]
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(body=_endpoint_body(events=events)))

    _call(enabled_events=events)
    body = parse_qs(stub_server.requests[0]["body"])
    assert body["enabled_events[]"] == events


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(code=400, body='{"error": {"message": "Invalid URL"}}'))
    with pytest.raises(ValueError, match="webhook endpoint create failed") as excinfo:
        _call()
    assert "400" in str(excinfo.value)


@pytest.mark.usefixtures("curl_app")
def test_livemode_mismatch_on_created_endpoint_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(livemode=True))  # test key, live endpoint -> mismatch
    with pytest.raises(StripeLivemodeMismatch):
        _call()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"url": ""}, "url"),
        ({"url": "http://acme.example/hook"}, "https"),
        ({"url": "https://"}, "https"),
        ({"url": "ftp://acme.example/hook"}, "https"),
        ({"enabled_events": []}, "enabled_events"),
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
    app = load_registrations("tai42_tools_stripe.tools.create_stripe_webhook_endpoint")
    assert set(app.tools.registered) == {"create_stripe_webhook_endpoint"}
