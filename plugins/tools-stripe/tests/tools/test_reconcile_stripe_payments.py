"""Tests for ``reconcile_stripe_payments``: the list filters and paging, session selection, the
disjoint-and-total outcome buckets, the per-session vs deployment-wide split, the pacing setting,
the lookback bound, and the page ceiling. The Stripe list and the callback door share one loopback
stub, routed by path."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_stripe.tools import reconcile_stripe_payments as recon_mod
from tai42_tools_stripe.tools.reconcile_stripe_payments import reconcile_stripe_payments

_WHITELIST = {"status", "session_id", "payment_intent", "amount_total", "currency", "customer_email"}


def _paid(session_id: str, callback_url: str, **overrides: Any) -> dict[str, Any]:
    session = {
        "id": session_id,
        "payment_intent": f"pi_{session_id}",
        "payment_status": "paid",
        "amount_total": 5000,
        "currency": "usd",
        "livemode": False,
        "customer_details": {"email": "b@example.com"},
        "metadata": {
            "tai_callback_url": callback_url,
            "tai_amount": "5000",
            "tai_currency": "usd",
        },
    }
    session.update(overrides)
    return session


def _list_page(sessions: list[dict[str, Any]], has_more: bool = False) -> str:
    return json.dumps({"object": "list", "data": sessions, "has_more": has_more})


def _cb(base: str, ticket: str) -> str:
    return f"{base}/api/interactions/callback/{ticket}"


def _router(list_pages: list[str], door: Callable[[dict[str, Any]], tuple[int, dict[str, str], str]]) -> Any:
    calls = {"list": 0}

    def responder(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        if request["path"] == "/v1/checkout/sessions":
            index = min(calls["list"], len(list_pages) - 1)
            calls["list"] += 1
            return 200, {"Content-Type": "application/json"}, list_pages[index]
        return door(request)

    return responder


def _door_by_ticket(mapping: dict[str, tuple[int, dict[str, str], str]]) -> Any:
    def door(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        ticket = request["path"].rsplit("/", 1)[-1]
        return mapping[ticket]

    return door


_ANSWERED = (200, {}, '{"data": {"status": "answered"}}')
_DUP = (200, {}, '{"data": {"status": "already_answered"}}')


def _assert_total(summary: dict[str, Any]) -> None:
    assert summary["selected"] == (
        summary["answered"]
        + summary["already_answered"]
        + summary["expired"]
        + summary["rejected"]
        + len(summary["failed"])
    )


@pytest.mark.usefixtures("curl_app")
def test_happy_path_lists_and_answers(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    session = _paid("cs1", _cb(stub_server.base_url, "fresh"))
    stub_server.set_responder(_router([_list_page([session])], _door_by_ticket({"fresh": _ANSWERED})))

    summary = asyncio.run(reconcile_stripe_payments())
    assert summary == {
        "selected": 1,
        "answered": 1,
        "already_answered": 0,
        "expired": 0,
        "rejected": 0,
        "failed": [],
    }

    list_request = next(r for r in stub_server.requests if r["path"] == "/v1/checkout/sessions")
    assert list_request["query"]["status"] == ["complete"]
    assert list_request["query"]["limit"] == ["100"]
    expected_gte = int(time.time()) - 26 * 3600
    assert abs(int(list_request["query"]["created[gte]"][0]) - expected_gte) <= 5

    door_request = next(r for r in stub_server.requests if r["path"].startswith("/api/interactions/callback/"))
    posted = json.loads(door_request["body"])
    assert set(posted) == _WHITELIST
    assert "metadata" not in posted
    assert posted["customer_email"] == "b@example.com"


@pytest.mark.usefixtures("curl_app")
def test_pagination_follows_cursor(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    page1 = _list_page([_paid("cs1", _cb(stub_server.base_url, "fresh"))], has_more=True)
    page2 = _list_page([_paid("cs2", _cb(stub_server.base_url, "fresh"))], has_more=False)
    stub_server.set_responder(_router([page1, page2], _door_by_ticket({"fresh": _ANSWERED})))

    summary = asyncio.run(reconcile_stripe_payments())
    assert summary["selected"] == 2
    assert summary["answered"] == 2
    list_requests = [r for r in stub_server.requests if r["path"] == "/v1/checkout/sessions"]
    assert list_requests[1]["query"]["starting_after"] == ["cs1"]


@pytest.mark.usefixtures("curl_app")
def test_selection_skips_unpaid_and_uncallbacked(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", _cb(stub_server.base_url, "fresh"), payment_status="unpaid"),
        _paid("cs3", _cb(stub_server.base_url, "fresh"), payment_status="no_payment_required"),
        _paid("cs4", "", metadata={"tai_amount": "5000"}),  # paid but no tai_callback_url
    ]
    stub_server.set_responder(_router([_list_page(sessions)], _door_by_ticket({"fresh": _ANSWERED})))

    summary = asyncio.run(reconcile_stripe_payments())
    assert summary["selected"] == 1
    assert summary["answered"] == 1
    assert len(summary["failed"]) == 0


@pytest.mark.usefixtures("curl_app")
def test_mixed_outcome_summary(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", _cb(stub_server.base_url, "dup")),
        _paid("cs3", _cb(stub_server.base_url, "gone")),
        _paid("cs4", _cb(stub_server.base_url, "bad")),
    ]
    door = _door_by_ticket({"fresh": _ANSWERED, "dup": _DUP, "gone": (404, {}, "gone"), "bad": (400, {}, "bad")})
    stub_server.set_responder(_router([_list_page(sessions)], door))

    summary = asyncio.run(reconcile_stripe_payments())
    assert summary == {
        "selected": 4,
        "answered": 1,
        "already_answered": 1,
        "expired": 1,
        "rejected": 1,
        "failed": [],
    }
    _assert_total(summary)


@pytest.mark.usefixtures("curl_app")
def test_door_403_lands_in_failed_and_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", _cb(stub_server.base_url, "forbid")),
    ]
    door = _door_by_ticket({"fresh": _ANSWERED, "forbid": (403, {}, "no")})
    stub_server.set_responder(_router([_list_page(sessions)], door))

    with pytest.raises(ValueError, match="failed"):
        asyncio.run(reconcile_stripe_payments())
    answered_door = [r for r in stub_server.requests if r["path"].endswith("/fresh")]
    assert len(answered_door) == 1


@pytest.mark.usefixtures("curl_app")
def test_unrecognised_200_lands_in_failed_and_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", _cb(stub_server.base_url, "weird")),
    ]
    door = _door_by_ticket({"fresh": _ANSWERED, "weird": (200, {}, '{"data": {"status": "something_else"}}')})
    stub_server.set_responder(_router([_list_page(sessions)], door))

    with pytest.raises(ValueError, match="failed"):
        asyncio.run(reconcile_stripe_payments())


@pytest.mark.usefixtures("curl_app")
def test_livemode_mismatch_lands_in_failed_not_rejected(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", _cb(stub_server.base_url, "cross"), livemode=True),
    ]
    stub_server.set_responder(_router([_list_page(sessions)], _door_by_ticket({"fresh": _ANSWERED})))

    with pytest.raises(ValueError, match="failed"):
        asyncio.run(reconcile_stripe_payments())


@pytest.mark.usefixtures("curl_app")
def test_ssrf_refusal_lands_in_failed(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", "https://evil.tld/api/interactions/callback/x"),
    ]
    stub_server.set_responder(_router([_list_page(sessions)], _door_by_ticket({"fresh": _ANSWERED})))

    with pytest.raises(ValueError, match="failed"):
        asyncio.run(reconcile_stripe_payments())


@pytest.mark.usefixtures("curl_app")
def test_fail_fast_verdict_raises_after_attempting_all(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sessions = [
        _paid("cs1", _cb(stub_server.base_url, "fresh")),
        _paid("cs2", _cb(stub_server.base_url, "unauth")),
    ]
    door = _door_by_ticket({"fresh": _ANSWERED, "unauth": (401, {}, "no")})
    stub_server.set_responder(_router([_list_page(sessions)], door))

    with pytest.raises(ValueError, match="failed"):
        asyncio.run(reconcile_stripe_payments())
    assert any(r["path"].endswith("/fresh") for r in stub_server.requests)


@pytest.mark.usefixtures("curl_app")
def test_pacing_waits_between_answers(
    stripe_env: Callable[..., None], stub_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=5.0,
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(recon_mod, "_sleep", fake_sleep)
    sessions = [_paid(f"cs{i}", _cb(stub_server.base_url, "fresh")) for i in range(3)]
    stub_server.set_responder(_router([_list_page(sessions)], _door_by_ticket({"fresh": _ANSWERED})))

    summary = asyncio.run(reconcile_stripe_payments())
    assert summary["answered"] == 3
    assert sleeps == [5.0, 5.0]


@pytest.mark.usefixtures("curl_app")
def test_zero_interval_disables_pacing(
    stripe_env: Callable[..., None], stub_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stripe_env(
        secret_key="sk_test_abc",
        api_base=stub_server.base_url,
        public_base=stub_server.base_url,
        bridge_secret="bridge-secret",
        interval=0,
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(recon_mod, "_sleep", fake_sleep)
    sessions = [_paid(f"cs{i}", _cb(stub_server.base_url, "fresh")) for i in range(3)]
    stub_server.set_responder(_router([_list_page(sessions)], _door_by_ticket({"fresh": _ANSWERED})))

    asyncio.run(reconcile_stripe_payments())
    assert sleeps == []


@pytest.mark.usefixtures("curl_app")
def test_lookback_hours_is_honored(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url, interval=0)
    stub_server.set_responder(_router([_list_page([])], _door_by_ticket({})))
    asyncio.run(reconcile_stripe_payments(lookback_hours=3))
    list_request = stub_server.requests[0]
    expected_gte = int(time.time()) - 3 * 3600
    assert abs(int(list_request["query"]["created[gte]"][0]) - expected_gte) <= 5


@pytest.mark.parametrize("lookback", [0, -1, 169])
def test_lookback_out_of_bounds_raises_before_stripe(
    stripe_env: Callable[..., None], stub_server: Any, lookback: int
) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url)
    with pytest.raises(ValueError, match=r"1\.\.168"):
        asyncio.run(reconcile_stripe_payments(lookback_hours=lookback))
    assert stub_server.requests == []


@pytest.mark.usefixtures("curl_app")
@pytest.mark.parametrize("lookback", [1, 168])
def test_lookback_bounds_are_inclusive(stripe_env: Callable[..., None], stub_server: Any, lookback: int) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url, interval=0)
    stub_server.set_responder(_router([_list_page([])], _door_by_ticket({})))
    summary = asyncio.run(reconcile_stripe_payments(lookback_hours=lookback))
    assert summary["selected"] == 0


@pytest.mark.usefixtures("curl_app")
def test_page_ceiling_raises(stripe_env: Callable[..., None], stub_server: Any) -> None:
    stripe_env(secret_key="sk_test_abc", api_base=stub_server.base_url, interval=0)
    counter = {"n": 0}

    def responder(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        counter["n"] += 1
        return 200, {}, _list_page([_paid(f"cs{counter['n']}", "https://x/y")], has_more=True)

    stub_server.set_responder(responder)
    with pytest.raises(ValueError, match="50-page ceiling"):
        asyncio.run(reconcile_stripe_payments())
    assert len([r for r in stub_server.requests if r["path"] == "/v1/checkout/sessions"]) == 50


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_stripe.tools.reconcile_stripe_payments")
    assert set(app.tools.registered) == {"reconcile_stripe_payments"}
