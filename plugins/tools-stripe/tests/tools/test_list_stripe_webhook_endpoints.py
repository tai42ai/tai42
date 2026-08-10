"""Tests for the ``list_stripe_webhook_endpoints`` tool: the returned shape (no signing secrets),
the ``starting_after`` pagination, the livemode assert on each listed endpoint, and the empty
listing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe._internal.tools.stripe_client import StripeLivemodeMismatch
from tai42_tools_stripe.tools.list_stripe_webhook_endpoints import list_stripe_webhook_endpoints


def _endpoint(endpoint_id: str, *, livemode: bool = False) -> dict[str, Any]:
    return {
        "id": endpoint_id,
        "object": "webhook_endpoint",
        "url": f"https://acme.example/hook/{endpoint_id}",
        "enabled_events": ["checkout.session.completed"],
        "status": "enabled",
        "livemode": livemode,
    }


def _page(endpoints: list[dict[str, Any]], *, has_more: bool = False) -> str:
    return json.dumps({"object": "list", "data": endpoints, "has_more": has_more})


def _call() -> dict[str, Any]:
    return asyncio.run(list_stripe_webhook_endpoints())


@pytest.mark.usefixtures("curl_app")
def test_happy_path_returns_shape_without_secrets(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(lambda _r: (200, {"Content-Type": "application/json"}, _page([_endpoint("we_1")])))

    result = _call()
    assert result == {
        "endpoints": [
            {
                "endpoint_id": "we_1",
                "url": "https://acme.example/hook/we_1",
                "status": "enabled",
                "enabled_events": ["checkout.session.completed"],
            }
        ]
    }
    assert "secret" not in result["endpoints"][0]

    request = stub_server.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/v1/webhook_endpoints"
    assert request["query"]["limit"] == ["100"]


@pytest.mark.usefixtures("curl_app")
def test_empty_listing(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(lambda _r: (200, {"Content-Type": "application/json"}, _page([])))
    assert _call() == {"endpoints": []}


@pytest.mark.usefixtures("curl_app")
def test_paginates_with_starting_after(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    calls = {"n": 0}

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {}, _page([_endpoint("we_a")], has_more=True)
        return 200, {}, _page([_endpoint("we_b")], has_more=False)

    stub_server.set_responder(responder)
    result = _call()
    assert [e["endpoint_id"] for e in result["endpoints"]] == ["we_a", "we_b"]
    assert stub_server.requests[1]["query"]["starting_after"] == ["we_a"]


@pytest.mark.usefixtures("curl_app")
def test_livemode_mismatch_on_listed_endpoint_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(
        lambda _r: (200, {"Content-Type": "application/json"}, _page([_endpoint("we_1", livemode=True)]))
    )
    with pytest.raises(StripeLivemodeMismatch):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.list_stripe_webhook_endpoints")
    assert set(app.tools.registered) == {"list_stripe_webhook_endpoints"}
