"""The ``create_stripe_webhook_endpoint`` tool: provision a Stripe webhook endpoint.

Registers an inbound webhook endpoint with Stripe through the same client seam and ``Stripe-Version``
pin as the checkout tools, and returns the endpoint id and its one-time signing secret. The Stripe
REST call lives in :mod:`tai42_tools_stripe._internal.tools.stripe_client`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from tai42_contract.app import tai42_app
from tai42_contract.secrets import SecretValue

from tai42_tools_stripe._internal.tools.stripe_client import (
    _assert_livemode,
    create_webhook_endpoint,
)


@tai42_app.tools.tool(tags={"stripe", "payments"})
async def create_stripe_webhook_endpoint(url: str, enabled_events: list[str]) -> dict[str, Any]:
    """Create a Stripe webhook endpoint and return its id and one-time signing secret.

    Security:
        The signing ``secret`` is revealed by Stripe ONLY on this create call and never again. This
        tool RETURNS it wrapped in a :class:`SecretValue` envelope -- repr-safe and not
        JSON-serializable, so it cannot leak through a log line, a recorder, or an LLM turn -- and
        writes it nowhere (no config, no env, no file). Reveal it with ``.reveal()`` to persist it
        through the platform's config API; a lost secret cannot be re-fetched and forces a
        create-new / delete-old rotation.

    Stripe has no rotate-secret endpoint: rotation is create a new endpoint, swap the stored secret
    to the new one, then delete the old endpoint -- the three webhook tools together enable it.

    ``url`` is where Stripe POSTs events TO us, so it is validated as an ``https`` URL with a host
    and is NOT run through the outbound-callback SSRF pin (that pin guards POSTs this process dials;
    this URL is dialed by Stripe, not by us). ``enabled_events`` must be a non-empty list of Stripe
    event types (e.g. ``["checkout.session.completed"]``). The created endpoint's ``livemode`` is
    asserted against the configured key's mode and a mismatch raises.

    Args:
        url: The https endpoint URL Stripe delivers events to. Required and non-empty.
        enabled_events: The Stripe event types to subscribe. Required and non-empty.

    Returns:
        ``{"endpoint_id", "secret", "url", "enabled_events", "status"}`` where ``secret`` is the
        one-time signing secret wrapped in a :class:`SecretValue`.
    """
    if not url:
        raise ValueError("url is required and must be non-empty")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"url must be an https URL with a host; got {url!r}")
    if not enabled_events:
        raise ValueError("enabled_events is required and must be a non-empty list of event types")

    endpoint = await create_webhook_endpoint(url=url, enabled_events=enabled_events)
    _assert_livemode(endpoint)
    return {
        "endpoint_id": endpoint["id"],
        "secret": SecretValue(endpoint["secret"]),
        "url": endpoint["url"],
        "enabled_events": endpoint["enabled_events"],
        "status": endpoint["status"],
    }
