"""Tests for the ``delete_stripe_webhook_endpoint`` tool: the DELETE call, the returned deletion
confirmation, the empty-id guard, and a Stripe non-2xx propagating as the client's error."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe.tools.delete_stripe_webhook_endpoint import delete_stripe_webhook_endpoint


def _responder(
    *, code: int = 200, body: str | None = None
) -> Callable[[dict[str, Any]], tuple[int, dict[str, str], str]]:
    payload = body if body is not None else '{"id": "we_1", "object": "webhook_endpoint", "deleted": true}'

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return code, {"Content-Type": "application/json"}, payload

    return responder


def _call(endpoint_id: str = "we_1") -> dict[str, Any]:
    return asyncio.run(delete_stripe_webhook_endpoint(endpoint_id))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_returns_deletion_confirmation(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = _call()
    assert result == {"endpoint_id": "we_1", "deleted": True}

    request = stub_server.requests[0]
    assert request["method"] == "DELETE"
    assert request["path"] == "/v1/webhook_endpoints/we_1"
    assert request["headers"]["authorization"] == "Bearer sk_test_abc"


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(code=404, body='{"error": {"message": "No such webhook endpoint"}}'))
    with pytest.raises(ValueError, match="webhook endpoint delete failed") as excinfo:
        _call()
    assert "404" in str(excinfo.value)


def test_empty_endpoint_id_raises() -> None:
    with pytest.raises(ValueError, match="endpoint_id"):
        _call("")


@pytest.mark.parametrize("secret_key", [None, ""])
def test_missing_or_empty_secret_key_raises(stripe_env: Callable[..., None], secret_key: str | None) -> None:
    stripe_env(secret_key=secret_key)
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.delete_stripe_webhook_endpoint")
    assert set(app.tools.registered) == {"delete_stripe_webhook_endpoint"}
