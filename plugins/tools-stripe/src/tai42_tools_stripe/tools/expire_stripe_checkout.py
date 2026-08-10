"""The ``expire_stripe_checkout`` tool: expire an issued Stripe Checkout Session early.

Ends an ``open`` Checkout Session before Stripe's ~24h default timeout through the same client seam
and ``Stripe-Version`` pin as :mod:`tai42_tools_stripe.tools.create_stripe_checkout`, and returns
the session id and its post-expire status. The Stripe REST call lives in
:mod:`tai42_tools_stripe._internal.tools.stripe_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import (
    _assert_livemode,
    expire_checkout_session,
)


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def expire_stripe_checkout(session_id: str) -> dict[str, Any]:
    """Expire an open Stripe Checkout Session early and return its id and post-expire status.

    Stripe expires only a session whose ``status`` is ``open`` -- a completed or already-expired
    session is a Stripe 400 that propagates, never a silent success. The returned session's
    ``livemode`` is asserted against the configured key's mode and a mismatch raises.

    Args:
        session_id: The Checkout Session id to expire (e.g. ``"cs_..."``). Required and non-empty.

    Returns:
        ``{"session_id": <session id>, "status": <post-expire status>}``.
    """
    if not session_id:
        raise ValueError("session_id is required and must be non-empty")

    session = await expire_checkout_session(session_id)
    _assert_livemode(session)
    return {"session_id": session["id"], "status": session["status"]}
