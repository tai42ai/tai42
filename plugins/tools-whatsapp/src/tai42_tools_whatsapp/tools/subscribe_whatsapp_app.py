"""The ``subscribe_whatsapp_app`` tool: subscribe the app to the account's webhooks.

Subscribes the app to the configured WhatsApp Business Account's webhooks through the Graph client
and returns Graph's response, optionally overriding the callback URI and verify token. The Graph
call and the pinned API base live in :mod:`tai42_tools_whatsapp._internal.tools.whatsapp_client`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from tai42_contract.app import tai42_app

from tai42_tools_whatsapp._internal.tools.whatsapp_client import subscribe_app


@tai42_app.tools.tool(tags={"whatsapp", "provisioning"})
async def subscribe_whatsapp_app(
    callback_uri: str | None = None,
    verify_token: str | None = None,
) -> dict[str, Any]:
    """Subscribe the app to the business account's webhooks and return Graph's response.

    With both ``callback_uri`` and ``verify_token`` supplied, the subscription overrides the app's
    configured webhook with Meta's ``override_callback_uri`` + ``verify_token`` pair; with neither,
    it subscribes to the app's configured webhook. Supplying exactly one of the two is a caller
    error and raises. When ``callback_uri`` is given it must be an ``https`` URL with a host —
    Meta's webhook contract accepts no other scheme.

    Args:
        callback_uri: Optional per-account webhook callback URL. Must be an https URL with a host
            and be given together with ``verify_token``.
        verify_token: Optional token echoed during Meta's verification handshake. Must be given
            together with ``callback_uri``.

    Returns:
        Graph's response JSON.
    """
    if callback_uri is not None:
        parsed = urlparse(callback_uri)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"callback_uri must be an https URL with a host; got {callback_uri!r}")
    return await subscribe_app(callback_uri, verify_token)
