"""The ``list_stripe_webhook_endpoints`` tool: enumerate the account's Stripe webhook endpoints.

Lists the configured webhook endpoints through the same client seam and ``Stripe-Version`` pin as
the checkout tools. The Stripe REST call and its pagination live in
:mod:`tai42_tools_stripe._internal.tools.stripe_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import (
    _assert_livemode,
    list_webhook_endpoints,
)


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def list_stripe_webhook_endpoints() -> dict[str, Any]:
    """List the account's Stripe webhook endpoints and return their id, url, status and events.

    Stripe never returns a signing ``secret`` on this call -- the secret is create-only, so a
    caller who lost it must rotate (create-new / delete-old), not re-list. Each endpoint's
    ``livemode`` is asserted against the configured key's mode and a mismatch raises.

    Returns:
        ``{"endpoints": [{"endpoint_id", "url", "status", "enabled_events"}, ...]}``.
    """
    endpoints = await list_webhook_endpoints()
    listed: list[dict[str, Any]] = []
    for endpoint in endpoints:
        _assert_livemode(endpoint)
        listed.append(
            {
                "endpoint_id": endpoint["id"],
                "url": endpoint["url"],
                "status": endpoint["status"],
                "enabled_events": endpoint["enabled_events"],
            }
        )
    return {"endpoints": listed}
