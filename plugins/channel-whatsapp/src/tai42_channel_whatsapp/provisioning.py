"""Reacting-form data-endpoint key provisioning (an operator step).

A reacting form's data endpoint decrypts each request's AES key with the business PRIVATE key
(held as the secret ``CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY``). Meta encrypts that AES key to the
matching PUBLIC key, which must be registered for the SENDING number once (or after a key
rotation). :func:`provision_flow_encryption` derives the public key from the configured private
key and registers it, so the operator never copies key material by hand. See the README for the
full operator steps (generate the RSA-2048 pair, set the secret, set the endpoint URI, run this).
"""

from __future__ import annotations

from tai42_channel_whatsapp.client import set_business_public_key
from tai42_channel_whatsapp.flow_crypto import load_private_key, public_key_pem
from tai42_channel_whatsapp.settings import require_delivery_secret, require_delivery_setting, whatsapp_settings


async def provision_flow_encryption(phone_number_id: str | None = None) -> None:
    """Register the business PUBLIC key (derived from the configured private key) for the sending number.

    ``phone_number_id`` defaults to ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID``. Raises
    ``ChannelDeliveryError`` when the private key or the sending number is unset (loud, never a
    silent skip), ``ValueError`` when the private key is not a readable RSA key, and surfaces any
    Graph API failure from the registration call.
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
    await set_business_public_key(number, public_key_pem(private_key))
