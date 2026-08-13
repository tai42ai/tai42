"""WhatsApp channel settings.

A ``TaiBaseSettings`` subclass reading the ``CHANNEL_WHATSAPP_`` env group
through accessors cached by ``tai42_kit.settings.settings_cache`` (dropped on a
soft restart). ``CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS`` whitelists
caller-requested destinations for ask_user. The access token, app secret, and
verify token are ``SecretStr`` (never in a repr/log/traceback); the plaintext is
read only at the Bearer-auth and signature-HMAC seams.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class WhatsAppSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_WHATSAPP_")

    # Graph API bearer token (Basic bearer for the send; one token serves many numbers).
    access_token: SecretStr | None = None
    # App secret keying the X-Hub-Signature-256 HMAC over inbound webhook bodies.
    app_secret: SecretStr | None = None
    # Shared secret echoed during Meta's GET webhook verification handshake.
    verify_token: SecretStr | None = None
    # Graph API origin with a pinned version; overridable so a stub can stand in (e2e).
    api_base_url: str = "https://graph.facebook.com/v23.0"
    # phone_number_id used as the sender when no sender_identity is routed (ask_user).
    default_phone_number_id: str | None = None
    # WhatsApp Business Account id that owns Flows; required only on the form-delivery
    # path (a form ask is rendered as a WhatsApp Flow created under this WABA).
    waba_id: str | None = None
    # Whitelist of wa_ids a caller-requested recipient must be on (ask_user only).
    # NoDecode hands the raw env string to the validator (comma-separated or JSON).
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Timeout for both the outbound Graph send and the loopback answer forward.
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long a handled wamid stays remembered (replay guard; 48h default).
    dedupe_ttl: int = Field(default=172_800, gt=0)
    # Rolling "seen within N days" window for template-send recipient policy: a
    # (phone_number_id, wa_id) pair the inbound webhook saw within this many days
    # is a KNOWN CONTACT a template may reach without being on the allowlist. 0
    # disables known-contact tracking entirely (allowlist-only template sends).
    template_contact_window_days: int = Field(default=30, ge=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """A list passes through; a string parses as JSON when it starts with
        ``[`` else splits on commas (items stripped, empties dropped); anything
        else is rejected loudly."""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                items = json.loads(text)
                for item in items:
                    if not isinstance(item, str):
                        raise ValueError(f"allowed_recipients JSON list items must be strings, got {item!r}")
                return [item for item in (part.strip() for part in items) if item]
            return [item for item in (part.strip() for part in text.split(",")) if item]
        raise ValueError("allowed_recipients must be a comma-separated string or a list")


@settings_cache
def whatsapp_settings() -> WhatsAppSettings:
    return WhatsAppSettings()


class WhatsAppRedisSettings(RedisConnectionSettings):
    """Connection for the plugin-owned correlation store (``CHANNEL_WHATSAPP_REDIS_URL`` etc.)."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_WHATSAPP_")


@settings_cache
def whatsapp_redis_settings() -> WhatsAppRedisSettings:
    return WhatsAppRedisSettings()


def require_delivery_setting(value: str | None, env_name: str) -> str:
    """The configured value an outbound send needs; raises ``ChannelDeliveryError``
    (naming only the env var) when unset. Checked before any network work."""
    if not value:
        raise ChannelDeliveryError(f"WhatsApp channel is not configured: set {env_name}.")
    return value


def require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """The plaintext secret an outbound send needs; raises ``ChannelDeliveryError``
    (naming only the env var, never the value) when unset."""
    secret = value.get_secret_value() if value is not None else ""
    if not secret:
        raise ChannelDeliveryError(f"WhatsApp channel is not configured: set {env_name}.")
    return secret
