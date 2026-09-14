"""Recipient resolution and the template allowlist fence.

Freeform sends go to any caller-supplied recipient (Meta's 24-hour window is the
fence); a TEMPLATE send is the one Meta delivers cold, so it keeps an operator
fence — the recipient must be allowlisted or a known contact.
"""

from __future__ import annotations

from tai42_contract.channels import ChannelDeliveryError, ChannelTemplate

from tai42_channel_whatsapp.client import send_template
from tai42_channel_whatsapp.correlation import is_known_contact
from tai42_channel_whatsapp.settings import WhatsAppSettings

_NO_DEFAULT_RECIPIENT = "no recipient requested and this channel has no default recipient; request a wa_id"


def _require_recipient(requested: str | None, message: str) -> str:
    """The caller-supplied recipient, or raise ``ChannelDeliveryError`` — this
    channel has no operator default recipient."""
    if requested is None:
        raise ChannelDeliveryError(message)
    return requested


async def _resolve_template_target(settings: WhatsAppSettings, phone_number_id: str, requested: str | None) -> str:
    """The recipient for a TEMPLATE send: required, and on the allowlist OR a
    known contact of the send-from ``phone_number_id``, else refused loudly.

    A template is the one send Meta delivers cold, so it keeps an operator fence;
    the known-contact lookup keys on the resolved send-from number — a participant is
    "known" to the number they actually messaged.
    """
    target = _require_recipient(requested, _NO_DEFAULT_RECIPIENT)
    if target in set(settings.allowed_recipients):
        return target
    if await is_known_contact(phone_number_id, target):
        return target
    raise ChannelDeliveryError(
        f"template send to {target!r} refused: not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS and not a known "
        f"contact of {phone_number_id} within the configured window"
    )


async def _send_template(
    settings: WhatsAppSettings, phone_number_id: str, target: str, template: ChannelTemplate
) -> list[str]:
    """Enforce the template recipient policy, then send the template."""
    resolved = await _resolve_template_target(settings, phone_number_id, target)
    return [await send_template(phone_number_id=phone_number_id, to=resolved, template=template)]
