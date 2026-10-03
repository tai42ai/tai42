"""The plugin's own configuration — the ``TAI_IDENTITY_*`` env namespace.

The provider owns its backing store, so it owns the store's configuration: the Redis
connection it reaches and the key namespace its identity records live under. The
contract injects no connection handle or key prefix — a provider reads them from its
own settings — so this module is where the Redis identity provider names them.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class RedisIdentityConnectionSettings(RedisConnectionSettings):
    """The provider's Redis connection, composed from the kit connection shape.

    Token validation runs on every request, so the connection tunes a short socket
    read timeout plus timeout-retry: a black-holed Redis fails fast instead of hanging
    the auth path. Connection values come from the ``TAI_IDENTITY_`` env
    (``TAI_IDENTITY_REDIS_URL`` …), falling back to ``TAI_DEFAULT_REDIS_URL`` when
    unset; only the resilience defaults are set here.
    """

    model_config = SettingsConfigDict(env_prefix="TAI_IDENTITY_")

    redis_url: str | None = None
    redis_max_connections: int | None = 10
    # Bound the connect phase too, so a black-holed Redis fails the auth path fast
    # instead of hanging on connect (``socket_timeout`` already bounds each read).
    # Must be positive.
    socket_connect_timeout: float | None = Field(default=5, gt=0)
    socket_timeout: float | None = 5
    retry_on_timeout: bool = True


class RedisIdentitySettings(TaiBaseSettings):
    """``TAI_IDENTITY_*`` configuration for the Redis identity provider."""

    model_config = SettingsConfigDict(env_prefix="TAI_IDENTITY_")

    # The namespace every stored identity record key carries
    # (``{key_prefix}{sha256(raw)}``); the validation read and the enumeration scan both
    # build on it, so it is a settings field, never a literal.
    key_prefix: str = "ac:key:"

    # Infra: the Redis connection is composed from the kit (a field, not a base), so this
    # config declares no connection fields of its own.
    redis: RedisIdentityConnectionSettings = Field(default_factory=RedisIdentityConnectionSettings)


@settings_cache
def redis_identity_settings() -> RedisIdentitySettings:
    """Return the process-wide :class:`RedisIdentitySettings`, cached after first load."""
    return RedisIdentitySettings()
