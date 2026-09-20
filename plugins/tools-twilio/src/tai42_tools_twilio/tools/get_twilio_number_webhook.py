"""The ``get_twilio_number_webhook`` tool: read one number's inbound-message webhook.

Reads a single IncomingPhoneNumber resource through the shared REST client and
projects it to its sid, phone number, and inbound-message webhook. The REST call
lives in :mod:`tai42_tools_twilio._internal.tools.twilio_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_twilio._internal.tools.twilio_client import get_incoming_phone_number


@tai42_app.tools.tool(tags={"twilio", "provisioning"})
async def get_twilio_number_webhook(phone_number_sid: str) -> dict[str, Any]:
    """Read one Twilio number's inbound-message webhook URL and method.

    Args:
        phone_number_sid: The IncomingPhoneNumber sid to read (e.g. ``"PN..."``).
            Required and non-empty.

    Returns:
        ``{"sid", "phone_number", "sms_url", "sms_method"}`` for the number.
    """
    if not phone_number_sid:
        raise ValueError("phone_number_sid is required and must be non-empty")

    number = await get_incoming_phone_number(phone_number_sid)
    return {
        "sid": number.get("sid"),
        "phone_number": number.get("phone_number"),
        "sms_url": number.get("sms_url"),
        "sms_method": number.get("sms_method"),
    }
