"""B4(a) — ``create_stripe_payment_link`` loaded on the payments stack.

``create_stripe_payment_link`` (the flexible-amount, non-blocking hosted link — no
ask/callback) is never loaded in any other manifest. The payments profile now carries it
alongside the checkout/confirm/reconcile trio, so one authed MCP call drives it against the
SAME in-process FakeStripe stub the checkout flow uses: no run-time call reaches a real
Stripe host (the tools' ``STRIPE_API_BASE`` points at the stub, the ``sk_test_`` key agrees
with the stub's ``livemode: false``).

Asserts the tool mints exactly one Checkout Session at the stub, stamps the money metadata,
and returns that session's hosted link + id. Deliberately NOT a real Stripe round trip
(PLAN_2's real/mock switch).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.netfixtures import FakeStripe
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# This asserts against the in-process FakeStripe stub (session mint + money metadata), so it
# is the stripe MOCK leg. A real stripe selection points the tool at the live Stripe host; the
# real leg is exercised on the dedicated e2e creds host (PLAN_2 §F), not in CI, so the
# stub-bound module steps aside for it. Inert in the default mock run — is_real("stripe") is
# False, so collection is byte-for-byte today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("stripe"),
    reason="in-process FakeStripe mint is the stripe mock leg; the real leg runs on the creds host (PLAN_2 §F)",
)

_AMOUNT = 4200
_CURRENCY = "usd"


async def test_create_stripe_payment_link_mints_a_session(
    payments_stack: tuple[TaiStack, str], fake_stripe: FakeStripe, uniq: Callable[[str], str]
) -> None:
    stack, root_token = payments_stack
    offer = uniq("offer")
    async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
        result = await mcp.call_tool(
            "create_stripe_payment_link",
            {
                "amount": _AMOUNT,
                "currency": _CURRENCY,
                "product_name": "Pro licence",
                "success_url": "https://acme.example/thanks",
                "cancel_url": "https://acme.example/cancelled",
                "offer_uuid": offer,
            },
            retry_on_reloading=True,
        )
    payload = result.data
    assert set(payload) == {"link", "session_id"}, payload

    # The returned session was really minted at the stub — assert membership by id rather
    # than a global count delta, so the check holds regardless of any other sessions the
    # session-scoped stub already carries (no reset / serial-ordering dependence).
    assert payload["session_id"] in fake_stripe.session_ids(), fake_stripe.session_ids()
    session = fake_stripe.session(payload["session_id"])
    assert session["amount_total"] == _AMOUNT, session
    assert session["currency"] == _CURRENCY, session
    # The tool stamps the money + offer metadata; the returned link is the stub's hosted URL.
    assert session["metadata"]["tai_amount"] == str(_AMOUNT), session
    assert session["metadata"]["tai_currency"] == _CURRENCY, session
    assert session["metadata"]["tai_offer_uuid"] == offer, session
    assert payload["link"] == session["url"], payload
