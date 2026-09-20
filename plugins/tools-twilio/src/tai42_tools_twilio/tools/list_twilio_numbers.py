"""The ``list_twilio_numbers`` tool: enumerate a Twilio account's incoming phone numbers.

Lists every IncomingPhoneNumber for the configured account through the shared REST
client, following Twilio's paging to exhaustion, and projects each to its sid,
phone number, and inbound-message webhook. The REST call lives in
:mod:`tai42_tools_twilio._internal.tools.twilio_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_twilio._internal.tools.twilio_client import list_incoming_phone_numbers


@tai42_app.tools.tool(tags={"twilio", "provisioning"})
async def list_twilio_numbers() -> list[dict[str, Any]]:
    """List the Twilio account's incoming phone numbers with their inbound-message webhook.

    Returns one entry per number, projected to the provisioning-relevant fields.

    Returns:
        A list of ``{"sid", "phone_number", "sms_url", "sms_method"}`` dicts, one
        per IncomingPhoneNumber on the account.
    """
    numbers = await list_incoming_phone_numbers()
    return [
        {
            "sid": number.get("sid"),
            "phone_number": number.get("phone_number"),
            "sms_url": number.get("sms_url"),
            "sms_method": number.get("sms_method"),
        }
        for number in numbers
    ]
