"""Tests for the shared Stripe client module: livemode prefixes, the exact-origin SSRF
pin, and the pinned ``Stripe-Version`` header on both REST calls.

The Stripe calls run through tai42-kit's real curl client against a loopback stub (an httpx-level
mock cannot intercept curl_cffi); no test reaches a Stripe host."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe._internal.tools.stripe_client import (
    STRIPE_API_VERSION,
    CallbackTargetRefused,
    StripeLivemodeMismatch,
    _assert_callback_target,
    _assert_livemode,
    _expected_livemode,
    create_checkout_session,
    list_checkout_sessions,
)

# --- livemode -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("sk_live_abc", True),
        ("rk_live_abc", True),
        ("sk_test_abc", False),
        ("rk_test_abc", False),
    ],
)
def test_expected_livemode_maps_all_four_prefixes(stripe_env: Callable[..., None], key: str, expected: bool) -> None:
    stripe_env(secret_key=key)
    assert _expected_livemode() is expected


@pytest.mark.parametrize("key", ["pk_live_abc", "whsec_abc", "garbage"])
def test_expected_livemode_raises_on_unrecognised_prefix(stripe_env: Callable[..., None], key: str) -> None:
    stripe_env(secret_key=key)
    with pytest.raises(ValueError, match="unrecognised key prefix"):
        _expected_livemode()


@pytest.mark.parametrize(
    ("key", "tail"),
    [
        ("pk_live_supersecrettail", "supersecrettail"),  # 2 underscores (3 segments)
        ("whsec_supersecrettail", "supersecrettail"),  # 1 underscore -- webhook secret pasted in
        ("garbagekeynounderscore", "garbagekeynounderscore"),  # 0 underscores -- whole value is secret
    ],
)
def test_unrecognised_prefix_message_does_not_leak_the_key(
    stripe_env: Callable[..., None], key: str, tail: str
) -> None:
    stripe_env(secret_key=key)
    with pytest.raises(ValueError, match="unrecognised key prefix") as excinfo:
        _expected_livemode()
    assert tail not in str(excinfo.value)


def test_assert_livemode_mismatch_raises(stripe_env: Callable[..., None]) -> None:
    stripe_env(secret_key="sk_test_abc")
    with pytest.raises(StripeLivemodeMismatch, match="livemode"):
        _assert_livemode({"livemode": True})


def test_assert_livemode_match_passes(stripe_env: Callable[..., None]) -> None:
    stripe_env(secret_key="sk_test_abc")
    _assert_livemode({"livemode": False})


@pytest.mark.parametrize("session", [{}, {"livemode": "true"}, {"livemode": 1}])
def test_assert_livemode_requires_boolean(stripe_env: Callable[..., None], session: dict[str, Any]) -> None:
    stripe_env(secret_key="sk_test_abc")
    with pytest.raises(ValueError, match="livemode"):
        _assert_livemode(session)


# --- SSRF pin -------------------------------------------------------------------------------

_BASE = "https://pay.acme.com"
_OK = "https://pay.acme.com/api/interactions/callback/tkt"


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://evil.tld/api/interactions/callback/t",  # foreign host
        "http://pay.acme.com/api/interactions/callback/t",  # different scheme
        "https://pay.acme.com:8443/api/interactions/callback/t",  # different port
        "https://pay.acme.com/admin",  # path outside the callback prefix
        "https://pay.acme.com.evil.tld/api/interactions/callback/t",  # suffix-extended host
        "https://pay.acme.com@evil.tld/api/interactions/callback/t",  # userinfo: real host is evil.tld
        "https://pay.acme.com/api/interactions/callback/../../admin",  # path traversal
    ],
)
def test_ssrf_pin_refuses(stripe_env: Callable[..., None], callback_url: str) -> None:
    stripe_env(public_base=_BASE)
    with pytest.raises(CallbackTargetRefused):
        _assert_callback_target(callback_url)


def test_ssrf_pin_accepts_exact_origin(stripe_env: Callable[..., None]) -> None:
    stripe_env(public_base=_BASE)
    _assert_callback_target(_OK)


def test_ssrf_pin_accepts_explicit_default_port(stripe_env: Callable[..., None]) -> None:
    stripe_env(public_base=_BASE)
    _assert_callback_target("https://pay.acme.com:443/api/interactions/callback/tkt")


def test_ssrf_pin_derives_prefix_from_path_bearing_base(stripe_env: Callable[..., None]) -> None:
    stripe_env(public_base="https://pay.acme.com/tai")
    _assert_callback_target("https://pay.acme.com/tai/api/interactions/callback/tkt")
    with pytest.raises(CallbackTargetRefused):
        _assert_callback_target("https://pay.acme.com/api/interactions/callback/tkt")


def test_ssrf_pin_raises_on_missing_base(stripe_env: Callable[..., None]) -> None:
    stripe_env(public_base="")
    with pytest.raises(ValueError, match="INTERACTIONS_PUBLIC_BASE_URL"):
        _assert_callback_target(_OK)


# --- Stripe-Version on both calls, and redirects off --------------------------------------


@pytest.mark.usefixtures("curl_app")
def test_stripe_version_header_on_create_and_list(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)

    def responder(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        if request["method"] == "POST":
            return 200, {"Content-Type": "application/json"}, '{"id": "cs_1", "url": "https://x"}'
        return 200, {"Content-Type": "application/json"}, '{"object": "list", "data": [], "has_more": false}'

    stub_server.set_responder(responder)

    asyncio.run(
        create_checkout_session(
            amount=5000,
            currency="usd",
            product_name="Pro",
            success_url="https://acme.example/thanks",
            cancel_url=None,
            metadata={"tai_callback_url": "https://acme.example/cb"},
            idempotency_basis="https://acme.example/cb",
        )
    )
    asyncio.run(list_checkout_sessions(1_700_000_000))

    assert len(stub_server.requests) == 2
    for request in stub_server.requests:
        assert request["headers"]["stripe-version"] == STRIPE_API_VERSION
        assert request["headers"]["authorization"] == "Bearer sk_test_abc"


@pytest.mark.usefixtures("curl_app")
def test_create_does_not_follow_redirect_and_hides_key(
    stripe_env: Callable[..., None], stub_server: Any, local_server: Any
) -> None:
    stripe_env(secret_key="sk_test_secretkey", api_base=stub_server.base_url)
    target = f"{local_server.base_url}/v1/checkout/sessions"
    stub_server.set_responder(lambda request: (302, {"Location": target}, ""))

    with pytest.raises(ValueError, match="HTTP 302") as excinfo:
        asyncio.run(
            create_checkout_session(
                amount=5000,
                currency="usd",
                product_name="Pro",
                success_url="https://acme.example/thanks",
                cancel_url=None,
                metadata={"tai_callback_url": "https://acme.example/cb"},
                idempotency_basis="https://acme.example/cb",
            )
        )
    # The redirect target recorded no request: the Authorization header never left the pinned host.
    assert local_server.requests == []
    assert "secretkey" not in str(excinfo.value)
    assert "Bearer" not in str(excinfo.value)


@pytest.mark.usefixtures("curl_app")
def test_create_raises_on_stripe_error_without_leaking_headers(
    stripe_env: Callable[..., None], stub_server: Any
) -> None:
    stripe_env(secret_key="sk_test_secretkey", api_base=stub_server.base_url)
    stub_server.set_responder(
        lambda request: (
            402,
            {"Content-Type": "application/json"},
            '{"error": {"message": "Your card was declined."}}',
        )
    )
    with pytest.raises(ValueError, match="402") as excinfo:
        asyncio.run(
            create_checkout_session(
                amount=5000,
                currency="usd",
                product_name="Pro",
                success_url="https://acme.example/thanks",
                cancel_url=None,
                metadata={"tai_callback_url": "https://acme.example/cb"},
                idempotency_basis="https://acme.example/cb",
            )
        )
    message = str(excinfo.value)
    assert "402" in message
    assert "declined" in message
    assert "secretkey" not in message


@pytest.mark.usefixtures("curl_app")
def test_list_page_ceiling_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    counter = {"n": 0}

    def responder(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        counter["n"] += 1
        page = json.dumps({"object": "list", "data": [{"id": f"cs_{counter['n']}"}], "has_more": True})
        return 200, {"Content-Type": "application/json"}, page

    stub_server.set_responder(responder)
    with pytest.raises(ValueError, match="50-page ceiling"):
        asyncio.run(list_checkout_sessions(1_700_000_000))
    assert len([r for r in stub_server.requests if r["method"] == "GET"]) == 50


@pytest.mark.usefixtures("curl_app")
def test_list_raises_on_non_2xx(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(lambda request: (500, {}, "stripe is down"))
    with pytest.raises(ValueError, match="list failed"):
        asyncio.run(list_checkout_sessions(1_700_000_000))


@pytest.mark.usefixtures("curl_app")
def test_list_paginates_with_starting_after(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    calls = {"n": 0}

    def responder(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {}, '{"object": "list", "data": [{"id": "cs_a"}], "has_more": true}'
        return 200, {}, '{"object": "list", "data": [{"id": "cs_b"}], "has_more": false}'

    stub_server.set_responder(responder)
    sessions = asyncio.run(list_checkout_sessions(1_700_000_000))
    assert [s["id"] for s in sessions] == ["cs_a", "cs_b"]
    assert stub_server.requests[1]["query"]["starting_after"] == ["cs_a"]
    for request in stub_server.requests:
        assert request["query"]["status"] == ["complete"]
        assert request["query"]["limit"] == ["100"]
