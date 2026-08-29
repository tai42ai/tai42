"""Telegram channel settings.

Two groups read the ``CHANNEL_TELEGRAM_`` prefix: :class:`TelegramSettings` (bot
credential, recipient allowlist + default, webhook secret, public base URL, HTTP
budget) and :class:`TelegramCorrelationSettings` (the correlation store's Redis
connection). Both are exposed through ``@settings_cache`` accessors (dropped on a
soft restart). Credentials are ``SecretStr`` (masked in repr/logs/model_dump);
fields default ``None`` so importing never demands config, and the kit's
``require``/``require_secret`` raise loudly at use, naming the missing env var.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class TelegramSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_TELEGRAM_")

    # The bot credential (from BotFather). SecretStr keeps it out of any repr/log;
    # the plaintext is read only when composing the Bot API URL (the token is the auth).
    bot_token: SecretStr | None = None
    # Operator whitelist of chats a caller-supplied recipient may name — a numeric
    # chat id (string) or ``@username``. The env value is comma-separated or a JSON
    # list (NoDecode -> the validator below). Gates only caller-supplied values.
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # The chat delivered to when the caller names none; trusted, not allowlist-checked.
    default_recipient: str | None = None
    # The setWebhook secret_token, echoed in X-Telegram-Bot-Api-Secret-Token; the
    # inbound door compares it constant-time and fails CLOSED when unset.
    webhook_secret: SecretStr | None = None
    # This deployment's public base URL; the startup hook builds the webhook URL from it.
    public_base_url: str | None = None
    # Bot API origin. Overridable so a stub can stand in (e2e); production never changes it.
    api_base_url: str = "https://api.telegram.org"
    # Wall-clock budget for one outbound HTTP call. Must be positive.
    http_timeout_seconds: float = Field(default=30, gt=0)
    # How long the tappable-option side record for a NOTIFY survives so a later tap
    # still resolves its option text into a bridged visitor message (a notify carries
    # no deadline of its own). A select ask uses its remaining answer budget instead.
    option_tap_ttl_seconds: int = Field(default=86_400, gt=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """Parse the allowlist from a JSON list (bracketed string), a
        comma-separated string, or a list; entries must be strings, stripped,
        empties dropped. Any other shape raises loudly."""
        if isinstance(value, str):
            stripped = value.strip()
            # JSON text opening with "[" parses to a list or raises loudly.
            value = json.loads(stripped) if stripped.startswith("[") else stripped.split(",")
        if not isinstance(value, list):
            raise ValueError("allowed_recipients must be a comma-separated string or a list of chat addresses")
        entries: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("allowed_recipients entries must be strings")
            entry = item.strip()
            if entry:
                entries.append(entry)
        return entries


class TelegramCorrelationSettings(RedisConnectionSettings):
    """The Redis connection the correlation store uses.

    Field names come from :class:`RedisConnectionSettings` under this prefix
    (``CHANNEL_TELEGRAM_REDIS_URL`` etc.) — the plugin owns its own keys and
    connection.
    """

    model_config = SettingsConfigDict(env_prefix="CHANNEL_TELEGRAM_")


@settings_cache
def telegram_settings() -> TelegramSettings:
    return TelegramSettings()


@settings_cache
def telegram_correlation_settings() -> TelegramCorrelationSettings:
    return TelegramCorrelationSettings()


def bot_numeric_id(token: str) -> str:
    """The bot's numeric id — the token's ``<digits>:<secret>`` prefix. A token
    with no ``:`` or a non-numeric prefix is malformed and raises loudly."""
    prefix, sep, _ = token.partition(":")
    if not sep or not prefix.isdigit():
        raise ValueError("CHANNEL_TELEGRAM_BOT_TOKEN is malformed; expected '<numeric bot id>:<secret>'")
    return prefix
