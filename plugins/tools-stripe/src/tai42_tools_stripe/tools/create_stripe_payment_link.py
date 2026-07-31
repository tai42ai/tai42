"""The ``create_stripe_payment_link`` tool: mint a flexible-amount, non-blocking payment link.

Creates a one-line ``mode=payment`` Checkout Session through the same client seam and
``Stripe-Version`` pin as :mod:`tai42_tools_stripe.tools.create_stripe_checkout`, and returns the
hosted payment URL and session id. Unlike the checkout tool this carries NO interaction/ask
callback: it is a fire-and-return link with no answer path.
"""

from __future__ import annotations

import json
from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import (
    _assert_livemode,
    create_checkout_session,
)


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def create_stripe_payment_link(
    amount: int,
    currency: str,
    product_name: str,
    success_url: str,
    cancel_url: str,
    wa_id: str | None = None,
    offer_uuid: str | None = None,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session and return its hosted payment link and session id.

    Security:
        Deployment-fenced to deterministic flow callers only. This tool is never exposed on any
        agent or user toolset -- its money-facing arguments (``amount``, ``currency``,
        ``success_url``, ``cancel_url``) are all caller-supplied, so a prompt-injected agent could
        charge any amount or redirect the payer.

    ``amount`` is in the currency's SMALLEST unit -- ``5000`` is $50.00 for USD and other
    two-decimal currencies; for zero-decimal currencies (JPY, KRW) it is in WHOLE units. ``amount
    >= 1`` is a sanity floor only; Stripe's real per-currency minimum is higher and rejects a
    smaller amount with its own 400.

    The session is stamped with ``tai_amount`` (the integer as a string) and ``tai_currency`` (the
    lowercase code); ``tai_wa_id`` and ``tai_offer_uuid`` are stamped only when supplied. The
    ``Idempotency-Key`` is derived from a canonical hash of every argument, so two calls with
    identical arguments return the SAME session -- distinct payments must differ in at least one
    argument (``wa_id`` or ``offer_uuid`` are the natural discriminators).

    Args:
        amount: The charge in the currency's smallest unit (whole units for zero-decimal
            currencies). Must be at least 1.
        currency: A 3-letter lowercase ISO currency code (e.g. ``"usd"``). Uppercase is rejected
            loudly: Stripe returns lowercase.
        product_name: The single line item's product name shown on the hosted page.
        success_url: The author's own thank-you page. Required and non-empty.
        cancel_url: The page a backing-out payer lands on. Required and non-empty.
        wa_id: Optional WhatsApp id, stamped into ``metadata.tai_wa_id`` when supplied.
        offer_uuid: Optional offer id, stamped into ``metadata.tai_offer_uuid`` when supplied.

    Returns:
        ``{"link": <hosted payment URL>, "session_id": <session id>}``.
    """
    if amount < 1:
        raise ValueError(f"amount must be at least 1 (currency's smallest unit); got {amount}")
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha() and currency.islower()):
        raise ValueError(f"currency must be a 3-letter lowercase ISO code; got {currency!r}")
    if not product_name:
        raise ValueError("product_name is required and must be non-empty")
    if not success_url:
        raise ValueError("success_url is required and must be non-empty")
    if not cancel_url:
        raise ValueError("cancel_url is required and must be non-empty")

    metadata = {
        "tai_amount": str(amount),
        "tai_currency": currency,
    }
    if wa_id is not None:
        metadata["tai_wa_id"] = wa_id
    if offer_uuid is not None:
        metadata["tai_offer_uuid"] = offer_uuid

    idempotency_basis = json.dumps(
        {
            "amount": amount,
            "currency": currency,
            "product_name": product_name,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "wa_id": wa_id,
            "offer_uuid": offer_uuid,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    session = await create_checkout_session(
        amount=amount,
        currency=currency,
        product_name=product_name,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        idempotency_basis=idempotency_basis,
    )
    _assert_livemode(session)
    return {"link": session["url"], "session_id": session["id"]}
