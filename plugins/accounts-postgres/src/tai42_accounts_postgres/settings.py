"""Plugin configuration — the ``TAI_ACCOUNTS_*`` env namespace.

``AccountsPgSettings`` (``TAI_ACCOUNTS_PG_*``) is the Postgres connection for the
user/session/invite store; ``AccountsSettings`` (``TAI_ACCOUNTS_*``) carries the
session/invite lifetimes, login-throttle knobs, argon2 concurrency bound, and the
bootstrap gate. Redis is not configured here — it comes through the injected
``settings.redis``.
"""

from __future__ import annotations

import logging
import os

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache

logger = logging.getLogger(__name__)

# argon2-verify concurrency default = this multiple of the CPU count.
_HASH_CONCURRENCY_CPU_MULTIPLE = 2


def _default_hash_concurrency() -> int:
    """A small multiple of the CPU count, falling to a loud floor when
    ``os.cpu_count()`` returns ``None``."""
    cpu = os.cpu_count()
    if cpu is not None:
        return _HASH_CONCURRENCY_CPU_MULTIPLE * cpu
    logger.warning(
        "os.cpu_count() returned None; defaulting login_hash_concurrency to the floor of %d",
        _HASH_CONCURRENCY_CPU_MULTIPLE,
    )
    return _HASH_CONCURRENCY_CPU_MULTIPLE


class AccountsPgSettings(PostgresConnectionSettings):
    """``TAI_ACCOUNTS_PG_*`` Postgres connection for the plugin's own tables.

    ``pg_db`` defaults to ``"tai"`` (the tables live in the platform database). No
    baked-in credential — supply the password via ``TAI_ACCOUNTS_PG_PASSWORD``.
    """

    model_config = SettingsConfigDict(env_prefix="TAI_ACCOUNTS_PG_")

    pg_db: str = "tai"


class AccountsSettings(TaiBaseSettings):
    """``TAI_ACCOUNTS_*`` behavior config for the accounts plugin."""

    model_config = SettingsConfigDict(env_prefix="TAI_ACCOUNTS_")

    # Idle expiry is the sliding window; absolute expiry is the hard cap from mint.
    session_idle_seconds: int = 86400
    session_absolute_seconds: int = 2592000

    # An unconsumed invite is valid this long from mint.
    invite_ttl_seconds: int = 259200

    # Login rate limiting (failures-only). Per-account: past ``threshold`` consecutive
    # failures, capped exponential backoff; a success resets. Per-IP: a fixed-window
    # failure count catching distributed stuffing.
    login_backoff_threshold: int = 5
    login_backoff_cap_seconds: int = 900
    login_ip_max_attempts: int = 30
    login_ip_window_seconds: int = 900

    # Cap on concurrent off-loop argon2 verifies; over-cap requests shed with a 503.
    login_hash_concurrency: int = Field(default_factory=_default_hash_concurrency)
    login_hash_wait_seconds: float = 2.0

    # Bootstrap gate. Secure-by-default: with neither field set the gate is ON with an
    # auto-generated token shared via Redis. ``bootstrap_open`` is the only ungated
    # config (a local/dev opt-out), never the default.
    bootstrap_token: SecretStr | None = None
    bootstrap_open: bool = False

    # Per-deployment namespace prefixed onto every plugin Redis key so a shared Redis
    # cannot cross-read. Defaults to ``pg_db`` (derived in model_post_init since a
    # pydantic default cannot reference another field); never a hardcoded literal.
    # Deployments sharing both one Redis and one ``pg_db`` must set distinct values.
    redis_key_prefix: str | None = None

    pg: AccountsPgSettings = Field(default_factory=AccountsPgSettings)

    def model_post_init(self, __context: object) -> None:
        # Derive the Redis namespace from ``pg_db`` when left unset.
        if self.redis_key_prefix is None:
            self.redis_key_prefix = self.pg.pg_db

    @property
    def key_prefix(self) -> str:
        """The resolved Redis namespace segment (never ``None`` post-init)."""
        if self.redis_key_prefix is None:  # pragma: no cover - invariant guard
            raise RuntimeError("redis_key_prefix was not derived in model_post_init")
        return self.redis_key_prefix


@settings_cache
def accounts_settings() -> AccountsSettings:
    """Return the process-wide :class:`AccountsSettings`, cached after first load."""
    return AccountsSettings()
