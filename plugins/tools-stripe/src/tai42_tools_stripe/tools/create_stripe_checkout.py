"""The ``create_stripe_checkout`` tool: mint a Stripe Checkout Session as an external-ask link.

Creates a one-line ``mode=payment`` Checkout Session through tai42-kit's curl client and returns
the Stripe-hosted payment URL, usable directly as the ``link`` builder for an external ask. The
Stripe REST call and its ``Stripe-Version`` pin live in
:mod:`tai42_tools_stripe._internal.tools.stripe_client`.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import create_checkout_session


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def create_stripe_checkout(
    callback_url: str,
    amount: int,
    currency: str,
    product_name: str,
    success_url: str,
    cancel_url: str | None = None,
) -> str:
    """Create a Stripe Checkout Session and return its hosted payment URL.

    ``amount`` is in the currency's SMALLEST unit -- ``5000`` is $50.00 for USD and other
    two-decimal currencies. For zero-decimal currencies (JPY, KRW and the rest of Stripe's
    zero-decimal list) the amount is in WHOLE units, so ``5000`` means ¥5000, not ¥50.00; assuming
    cents everywhere overcharges a zero-decimal customer 100x. ``amount >= 1`` is a sanity floor
    only -- Stripe's real per-currency minimum is much higher (around 50 minor units for USD) and
    an amount below it is rejected by Stripe with its own 400.

    ``success_url`` is REQUIRED and is the author's own post-payment page -- a platform design
    choice, not a Stripe API rule (Stripe does not require one for hosted Checkout). It is NEVER
    the callback URL: the payer's browser sees only the Stripe-hosted page and then ``success_url``.
    The callback URL travels only inside ``metadata.tai_callback_url`` and is never a redirect
    target. ``cancel_url`` is the optional page a backing-out payer lands on.

    The session is stamped with three metadata keys -- ``tai_callback_url`` (the ask's ticket URL),
    ``tai_amount`` (the integer as a string) and ``tai_currency`` (the lowercase code). The longest
    is ``tai_callback_url``: a ~43-char token plus the public base URL, comfortably inside Stripe's
    500-char metadata-value cap for any realistic base URL.

    ``amount_total == unit_amount`` holds only because ``quantity`` is fixed at 1 and the session
    carries no tax, discount or promotion code -- ``amount_total`` is Stripe's post-adjustment
    total. The ask pins ``amount_total`` while this tool sends ``unit_amount``; any future
    ``quantity`` param, or automatic tax/coupons, would make them differ and 400 every payment
    against an existing preset. Such a parameter must derive the pin from the expected total or drop
    the const pin -- it is not a free addition.

    Args:
        callback_url: The external ask's callback URL; stamped into ``metadata.tai_callback_url``
            and used verbatim, never shown to the payer.
        amount: The charge in the currency's smallest unit (whole units for zero-decimal
            currencies). Must be at least 1.
        currency: A 3-letter lowercase ISO currency code (e.g. ``"usd"``). Uppercase is rejected
            loudly: Stripe returns lowercase, so an uppercase pin would 400 every answer.
        product_name: The single line item's product name shown on the hosted page.
        success_url: The author's own thank-you page. Required and non-empty; never the callback URL.
        cancel_url: Optional page for a payer who abandons the hosted checkout.

    Returns:
        The Stripe-hosted payment URL for the session.
    """
    if amount < 1:
        raise ValueError(f"amount must be at least 1 (currency's smallest unit); got {amount}")
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha() and currency.islower()):
        raise ValueError(f"currency must be a 3-letter lowercase ISO code; got {currency!r}")
    if not success_url:
        raise ValueError("success_url is required and must be non-empty")

    metadata = {
        "tai_callback_url": callback_url,
        "tai_amount": str(amount),
        "tai_currency": currency,
    }
    session = await create_checkout_session(
        amount=amount,
        currency=currency,
        product_name=product_name,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        idempotency_basis=callback_url,
    )
    return session["url"]
