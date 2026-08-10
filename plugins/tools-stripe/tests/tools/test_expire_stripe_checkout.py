"""Tests for the ``expire_stripe_checkout`` tool: the POST to the expire endpoint, the returned
id/status, the livemode assert on the returned session, the empty ``session_id`` guard, and a
Stripe 400 on a non-open session propagating as the client's error."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe._internal.tools.stripe_client import STRIPE_API_VERSION, StripeLivemodeMismatch
from tai42_tools_stripe.tools.expire_stripe_checkout import expire_stripe_checkout


def _session(*, status: str = "expired", livemode: bool = False) -> str:
    return f'{{"id": "cs_1", "status": "{status}", "livemode": {"true" if livemode else "false"}}}'


def _responder(
    *, code: int = 200, status: str = "expired", livemode: bool = False, body: str | None = None
) -> Callable[[dict[str, Any]], tuple[int, dict[str, str], str]]:
    payload = body if body is not None else _session(status=status, livemode=livemode)

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return code, {"Content-Type": "application/json"}, payload

    return responder


def _call(session_id: str = "cs_1") -> dict[str, Any]:
    return asyncio.run(expire_stripe_checkout(session_id))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_expires_and_returns_id_and_status(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = _call()
    assert result == {"session_id": "cs_1", "status": "expired"}

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/checkout/sessions/cs_1/expire"

    headers = request["headers"]
    assert headers["authorization"] == "Bearer sk_test_abc"
    assert headers["stripe-version"] == STRIPE_API_VERSION


@pytest.mark.usefixtures("curl_app")
def test_non_open_session_400_propagates(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(
        _responder(
            code=400,
            body='{"error": {"message": "You can only expire sessions with a status of open."}}',
        )
    )
    with pytest.raises(ValueError, match="expire failed") as excinfo:
        _call()
    message = str(excinfo.value)
    assert "400" in message
    assert "status of open" in message


@pytest.mark.usefixtures("curl_app")
def test_livemode_mismatch_on_returned_session_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    stub_server.set_responder(_responder(livemode=True))  # test key, live session -> mismatch
    with pytest.raises(StripeLivemodeMismatch):
        _call()


def test_empty_session_id_raises() -> None:
    with pytest.raises(ValueError, match="session_id"):
        _call("")


@pytest.mark.parametrize("secret_key", [None, ""])
def test_missing_or_empty_secret_key_raises(stripe_env: Callable[..., None], secret_key: str | None) -> None:
    stripe_env(secret_key=secret_key)
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.expire_stripe_checkout")
    assert set(app.tools.registered) == {"expire_stripe_checkout"}
