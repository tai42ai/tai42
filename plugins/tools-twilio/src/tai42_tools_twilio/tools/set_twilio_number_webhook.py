"""The ``set_twilio_number_webhook`` tool: point one number's inbound webhook at a URL.

Updates a single IncomingPhoneNumber's ``SmsUrl`` through the shared REST client
and returns the updated sid, phone number, and webhook URL. The REST call lives in
:mod:`tai42_tools_twilio._internal.tools.twilio_client`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from tai42_contract.app import tai42_app

from tai42_tools_twilio._internal.tools.twilio_client import update_incoming_phone_number_sms_url


@tai42_app.tools.tool(tags={"twilio", "provisioning"})
async def set_twilio_number_webhook(phone_number_sid: str, sms_url: str) -> dict[str, Any]:
    """Set a Twilio number's inbound-message webhook to a given https URL.

    Twilio POSTs each inbound message to this URL; requiring ``https`` keeps the
    message payload off cleartext transport.

    Args:
        phone_number_sid: The IncomingPhoneNumber sid to update (e.g. ``"PN..."``).
            Required and non-empty.
        sms_url: The inbound-message webhook URL. Required, non-empty, and https.

    Returns:
        ``{"sid", "phone_number", "sms_url"}`` for the updated number.
    """
    if not phone_number_sid:
        raise ValueError("phone_number_sid is required and must be non-empty")
    if not sms_url:
        raise ValueError("sms_url is required and must be non-empty")
    parsed = urlparse(sms_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"sms_url must be an https URL with a host; got {sms_url!r}")

    number = await update_incoming_phone_number_sms_url(phone_number_sid, sms_url)
    return {
        "sid": number.get("sid"),
        "phone_number": number.get("phone_number"),
        "sms_url": number.get("sms_url"),
    }
