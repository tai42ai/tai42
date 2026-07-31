"""Slack channel settings.

Two env groups under the ``CHANNEL_SLACK_`` prefix: channel credentials and
recipient policy (``SlackSettings``) and the correlation-store connection
(``SlackRedisSettings``). Both are reached through ``settings_cache`` accessors,
so a settings reset drops them and a rotated token/secret is picked up on the
next call. A caller-requested recipient must be on ``allowed_recipients`` (unlisted
refused loudly, fail closed) else ``default_recipient``. Fields default unset so
importing never demands config; the ``require``/``require_secret`` helpers raise
loudly naming the missing env var.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class SlackSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_SLACK_")

    # The bot token (``xoxb-…``, scope ``chat:write``). SecretStr keeps it out of
    # any repr/log; the plaintext is read only when building the Authorization header.
    bot_token: SecretStr | None = None
    # The Events API signing secret keying the ``v0`` HMAC-SHA256 scheme.
    signing_secret: SecretStr | None = None
    # The app's own bot user id (``U…``): the bridge's ``our_identity`` and the
    # self-message filter. Required only when a bridge route targets slack.
    bot_user_id: str | None = None
    # Operator whitelist of conversation ids (``C…``/``D…``) a caller-requested
    # recipient may name; unlisted is refused (fail closed). NoDecode hands the raw
    # env string to the validator (comma-separated or JSON list).
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # The id a question without a caller-requested recipient goes to; trusted, not
    # allowlist-checked. Unset plus no requested recipient raises at delivery time.
    default_recipient: str | None = None
    # Slack Web API origin. Overridable so a stub can stand in (e2e); production
    # never changes it.
    api_base_url: str = "https://slack.com/api"
    # Wall-clock budget for one outbound HTTP call. Must be positive.
    http_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """Parse the allowlist from a JSON list (bracketed string), a
        comma-separated string, or a list; string items are stripped and empties
        dropped, a non-string item passes through so item validation raises."""
        if isinstance(value, str):
            stripped = value.strip()
            value = json.loads(stripped) if stripped.startswith("[") else stripped.split(",")
        if isinstance(value, list):
            normalized: list[object] = []
            for item in value:
                if isinstance(item, str):
                    item = item.strip()
                    if not item:
                        continue
                normalized.append(item)
            return normalized
        raise ValueError("allowed_recipients must be a comma-separated string or a list")


class SlackRedisSettings(RedisConnectionSettings):
    """The correlation store connection — ``CHANNEL_SLACK_REDIS_URL`` plus the
    inherited tuning fields."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_SLACK_")


@settings_cache
def slack_settings() -> SlackSettings:
    return SlackSettings()


@settings_cache
def slack_redis_settings() -> SlackRedisSettings:
    return SlackRedisSettings()


def require[T](value: T | None, env_name: str) -> T:
    """The configured value, or raise naming the missing env var (a manifest-named
    channel missing config must fail loudly at use, never a silent no-op)."""
    if value is None:
        raise ValueError(f"the slack channel is not configured: set {env_name}")
    return value


def require_secret(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or raise on unset/EMPTY — fail CLOSED. An empty
    signing secret would make the inbound HMAC forgeable, so both raise."""
    secret = require(value, env_name).get_secret_value()
    if not secret:
        raise ValueError(f"{env_name} is set but empty")
    return secret
