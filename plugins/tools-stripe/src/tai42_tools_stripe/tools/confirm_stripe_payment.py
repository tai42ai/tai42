"""The ``confirm_stripe_payment`` tool: the webhook bridge that answers a paid ask.

Validates a projected ``checkout.session.completed`` event and POSTs the whitelisted answer to the
interaction callback door. The projection, the livemode assert, the SSRF pin and the bounded retry
live in :mod:`tai42_tools_stripe._internal.tools.stripe_client`.

This is a hook target and is NOT a user or agent tool: it holds the bridge secret and answers
payment asks. It runs in a background hook, so every failure is a loud raise -- a swallowed error
would strand a paying customer.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import (
    _assert_livemode,
    build_answer_payload,
    post_answer,
)


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def confirm_stripe_payment(event: dict[str, Any]) -> dict[str, Any]:
    """Answer a payment ask from a projected ``checkout.session.completed`` event.

    The single ``event`` param is required: a var-keyword signature cannot register as a tool, and
    the hook's canonical ``expr`` is what puts an ``event`` key into the tool kwargs. Only a
    ``paid`` session answers -- ``unpaid`` (async payment methods) and ``no_payment_required``
    (zero-total sessions) are rejected loudly by name. The later ``async_payment_succeeded`` event
    is out of scope.

    A door failure PROPAGATES unchanged (a ``CallbackDoorError`` of any status, or retry
    exhaustion) into the hook-failure log -- the bridge has one session and no batch to classify
    into, so it never catches. ``already_answered`` from the door is a SUCCESS return: Stripe
    retries are expected duplicates.

    Args:
        event: The projected Stripe event: ``{type, id, session}`` where ``session`` carries
            ``id``, ``payment_intent``, ``payment_status``, ``amount_total``, ``currency``,
            ``livemode``, ``customer_email`` and ``metadata``.

    Returns:
        The callback door's response body.
    """
    if event.get("type") != "checkout.session.completed":
        raise ValueError(f"event type must be 'checkout.session.completed'; got {event.get('type')!r}")
    session = event.get("session")
    if not isinstance(session, dict):
        raise ValueError("event 'session' is missing or not an object")

    payment_status = session.get("payment_status")
    if payment_status != "paid":
        raise ValueError(
            f"payment_status is {payment_status!r}; only 'paid' answers the ask -- "
            "'unpaid' (async payment methods) and 'no_payment_required' (zero-total sessions) do not"
        )

    _assert_livemode(session)

    metadata = session.get("metadata") or {}
    callback_url = metadata.get("tai_callback_url")
    if not callback_url:
        raise ValueError("session metadata.tai_callback_url is missing or empty")

    stamp_amount = metadata.get("tai_amount")
    if stamp_amount is None or int(stamp_amount) != session.get("amount_total"):
        raise ValueError(
            f"amount_total {session.get('amount_total')!r} disagrees with metadata.tai_amount {stamp_amount!r}"
        )
    stamp_currency = metadata.get("tai_currency")
    if stamp_currency is None or stamp_currency.lower() != str(session.get("currency")).lower():
        raise ValueError(
            f"currency {session.get('currency')!r} disagrees with metadata.tai_currency {stamp_currency!r}"
        )

    payload = build_answer_payload(session)
    return await post_answer(callback_url, payload)
