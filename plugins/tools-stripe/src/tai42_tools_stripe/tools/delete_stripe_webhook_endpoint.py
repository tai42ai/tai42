"""The ``delete_stripe_webhook_endpoint`` tool: remove a Stripe webhook endpoint.

Deletes a webhook endpoint by id through the same client seam and ``Stripe-Version`` pin as the
checkout tools. The Stripe REST call lives in
:mod:`tai42_tools_stripe._internal.tools.stripe_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_stripe._internal.tools.stripe_client import delete_webhook_endpoint


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def delete_stripe_webhook_endpoint(endpoint_id: str) -> dict[str, Any]:
    """Delete a Stripe webhook endpoint by id and return Stripe's deletion confirmation.

    This is the old-endpoint teardown of a secret rotation (create-new / swap-config / delete-old).
    A non-existent or already-deleted id is a Stripe error that propagates, never a silent success.

    Args:
        endpoint_id: The webhook endpoint id to delete (e.g. ``"we_..."``). Required and non-empty.

    Returns:
        ``{"endpoint_id": <deleted id>, "deleted": <bool>}``.
    """
    if not endpoint_id:
        raise ValueError("endpoint_id is required and must be non-empty")

    result = await delete_webhook_endpoint(endpoint_id)
    return {"endpoint_id": result["id"], "deleted": result["deleted"]}
