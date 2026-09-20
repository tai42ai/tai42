"""Tests for ``post_answer``'s bounded transient retry, pinned by REQUEST COUNT at the loopback
door (never by elapsed time). The backoff ``_sleep`` is monkeypatched so the suite never waits, and
the jitter source is seeded so a computed delay lands in a known range."""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe._internal.tools import stripe_client
from tai42_tools_stripe._internal.tools.stripe_client import CallbackDoorError, post_answer

_PAYLOAD = {"status": "paid", "amount_total": 5000, "currency": "usd"}
_OK_BODY = '{"data": {"status": "answered"}}'


def _callback_url(base: str) -> str:
    return f"{base}/api/interactions/callback/tkt"


def _sequence(*responses: tuple[int, dict[str, str], str]) -> Callable[[dict[str, Any]], Any]:
    calls = {"n": 0}

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[index]

    return responder


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(stripe_client, "_sleep", fake_sleep)
    monkeypatch.setattr(stripe_client, "_rng", random.Random(1234))
    return sleeps


@pytest.fixture
def door(stripe_env: Callable[..., None], stub_server: Any) -> Any:
    stripe_env(public_base=stub_server.base_url, bridge_secret="bridge-secret")
    return stub_server


def _run(base: str) -> dict[str, Any]:
    return asyncio.run(post_answer(_callback_url(base), _PAYLOAD))


@pytest.mark.usefixtures("curl_app")
@pytest.mark.parametrize("transient_status", [500, 503, 408, 429])
def test_transient_then_success_retries_once(door: Any, recorded_sleeps: list[float], transient_status: int) -> None:
    door.set_responder(_sequence((transient_status, {}, "busy"), (200, {}, _OK_BODY)))
    result = _run(door.base_url)
    assert result == {"data": {"status": "answered"}}
    assert len(door.requests) == 2
    assert len(recorded_sleeps) == 1


@pytest.mark.usefixtures("curl_app")
def test_retry_after_honoured_in_full(door: Any, recorded_sleeps: list[float]) -> None:
    door.set_responder(_sequence((429, {"Retry-After": "2"}, "slow"), (200, {}, _OK_BODY)))
    _run(door.base_url)
    assert recorded_sleeps == [2.0]


@pytest.mark.usefixtures("curl_app")
def test_retry_after_longer_than_budget_honoured_in_full(door: Any, recorded_sleeps: list[float]) -> None:
    door.set_responder(_sequence((429, {"Retry-After": "45"}, "slow"), (200, {}, _OK_BODY)))
    _run(door.base_url)
    assert recorded_sleeps == [45.0]


@pytest.mark.usefixtures("curl_app")
@pytest.mark.parametrize("retry_after", ["100000", "-5", "not-a-number", "Wed, 21 Oct 2099 07:28:00 GMT"])
def test_unusable_retry_after_falls_back_to_computed_backoff(
    door: Any, recorded_sleeps: list[float], retry_after: str
) -> None:
    door.set_responder(_sequence((429, {"Retry-After": retry_after}, "slow"), (200, {}, _OK_BODY)))
    _run(door.base_url)
    assert len(recorded_sleeps) == 1
    # Attempt-0 computed delay is 0.5s plus up to 10% jitter -- never the header value.
    assert 0.5 <= recorded_sleeps[0] <= 0.55


@pytest.mark.usefixtures("curl_app")
@pytest.mark.parametrize("verdict_status", [400, 401, 403, 404])
def test_verdict_status_fails_fast(door: Any, recorded_sleeps: list[float], verdict_status: int) -> None:
    door.set_responder(lambda request: (verdict_status, {}, "no"))
    with pytest.raises(CallbackDoorError) as excinfo:
        _run(door.base_url)
    assert excinfo.value.status == verdict_status
    assert len(door.requests) == 1
    assert recorded_sleeps == []


@pytest.mark.usefixtures("curl_app")
def test_callback_door_error_carries_status_body_and_pinned_message(door: Any, recorded_sleeps: list[float]) -> None:
    door.set_responder(lambda request: (404, {}, "gone"))
    with pytest.raises(CallbackDoorError) as excinfo:
        _run(door.base_url)
    exc = excinfo.value
    assert exc.status == 404
    assert exc.body == "gone"
    assert str(exc).startswith("callback door refused the answer: HTTP 404")
    assert "gone" in str(exc)


@pytest.mark.usefixtures("curl_app")
def test_transient_forever_raises_after_cap(door: Any, recorded_sleeps: list[float]) -> None:
    door.set_responder(lambda request: (500, {}, "down"))
    with pytest.raises(ValueError, match="after 5 attempts"):
        _run(door.base_url)
    assert len(door.requests) == 5
    assert len(recorded_sleeps) == 4


@pytest.mark.usefixtures("curl_app")
def test_redirect_is_not_followed_and_hides_secret(door: Any, recorded_sleeps: list[float], local_server: Any) -> None:
    target = f"{local_server.base_url}/api/interactions/callback/tkt"
    door.set_responder(lambda request: (302, {"Location": target}, ""))
    with pytest.raises(CallbackDoorError) as excinfo:
        _run(door.base_url)
    assert excinfo.value.status == 302
    # The redirect target recorded no request: X-TAI-Bridge-Secret never left the pinned origin.
    assert local_server.requests == []


@pytest.mark.usefixtures("curl_app")
def test_transport_error_retries_then_raises(stripe_env: Callable[..., None], recorded_sleeps: list[float]) -> None:
    # A closed loopback port refuses the connection: a CurlError on every attempt, so the ladder
    # retries to its cap and then raises rather than swallowing the transport failure.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    base = f"http://127.0.0.1:{port}"
    stripe_env(public_base=base, bridge_secret="bridge-secret")
    with pytest.raises(ValueError, match="after 5 attempts"):
        _run(base)
    assert len(recorded_sleeps) == 4


@pytest.mark.usefixtures("curl_app")
def test_missing_bridge_secret_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(public_base=stub_server.base_url, bridge_secret="")
    with pytest.raises(ValueError, match="TAI_BRIDGE_CALLBACK_SECRET"):
        _run(stub_server.base_url)
    assert stub_server.requests == []
