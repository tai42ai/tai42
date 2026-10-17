"""Reacting-form data-endpoint key provisioning (an operator step).

A reacting form's data endpoint decrypts each request's AES key with the business PRIVATE key
(held as the secret ``CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY``). Meta encrypts that AES key to the
matching PUBLIC key, which must be registered for each SENDING number once (or after a key
rotation). :func:`flow_encryption_material` derives the public key from the configured private
key and :func:`provision_flow_encryption` registers it, so the operator never copies key
material by hand. The operator's door is the admin-only
``POST /api/channels/whatsapp/flow-encryption`` route (:mod:`.provisioning_route`); see the
README for the full operator steps.
"""

from __future__ import annotations

from typing import NamedTuple

from tai42_channel_whatsapp.client import set_business_public_key
from tai42_channel_whatsapp.flow_crypto import load_private_key, public_key_pem
from tai42_channel_whatsapp.settings import require_delivery_secret, require_delivery_setting, whatsapp_settings


class FlowEncryptionMaterial(NamedTuple):
    """The sending number and the PEM public key to register for it."""

    phone_number_id: str
    public_key_pem: str


def flow_encryption_material(phone_number_id: str | None = None) -> FlowEncryptionMaterial:
    """The sending number and the business PUBLIC key derived from the configured private key.

    ``phone_number_id`` defaults to ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID``. No network
    work. Raises ``ChannelDeliveryError`` when the private key or the sending number is unset
    (loud, never a silent skip) and ``ValueError`` when the private key is not a readable RSA key.
    """
    settings = whatsapp_settings()
    private_pem = require_delivery_secret(settings.flow_private_key, "CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY")
    number = phone_number_id or require_delivery_setting(
        settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
    )
    passphrase = (
        settings.flow_private_key_passphrase.get_secret_value()
        if settings.flow_private_key_passphrase is not None
        else None
    )
    private_key = load_private_key(private_pem, passphrase)
    return FlowEncryptionMaterial(phone_number_id=number, public_key_pem=public_key_pem(private_key))


async def provision_flow_encryption(phone_number_id: str | None = None) -> None:
    """Register the business PUBLIC key (derived from the configured private key) for the sending number.

    Raises what :func:`flow_encryption_material` raises, before any network work, and surfaces
    any Graph API failure of the registration call as ``ChannelDeliveryError``.
    """
    material = flow_encryption_material(phone_number_id)
    await set_business_public_key(material.phone_number_id, material.public_key_pem)
