"""Pydantic-settings for the access-control policy store's Postgres connection.

The policy store is a platform primitive with its own ``ACCESS_CONTROL_STORE_*``
namespace, kept separate from the connector / versioning stores so each durable
store declares its own DSN. It targets the same ``tai`` database by default; a
deployment points every store at its Postgres via the shared env.

This namespace is PURELY the PG connection config — it is NOT a store selector.
Postgres is the only policy store; there is nothing to select.
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.settings import settings_cache


class AccessControlStorePgSettings(PostgresConnectionSettings):
    """``ACCESS_CONTROL_STORE_*`` Postgres connection for
    ``access_control_policies`` + ``access_control_routes``. No baked-in
    credential — supply the password via ``ACCESS_CONTROL_STORE_PG_PASSWORD``."""

    model_config = SettingsConfigDict(env_prefix="ACCESS_CONTROL_STORE_")

    pg_db: str = "tai"


@settings_cache
def access_control_store_settings() -> AccessControlStorePgSettings:
    return AccessControlStorePgSettings()


def access_control_store_configured() -> bool:
    """Whether this deployment configures the access-control policy store at all.

    Resolved through the SAME pydantic-settings the store connects with (its own
    ``ACCESS_CONTROL_STORE_*`` env or the shared ``TAI_DEFAULT_PG_PASSWORD``), read
    fresh — not the cached singleton — so a config reload re-evaluates. The store
    carries no baked-in credential, so a supplied password is the signal a real
    store is wired up. Mirrors ``connectors_store_configured`` / the other store
    gates: a bare env-var read would miss the ``TAI_DEFAULT_*`` fallback."""
    s = AccessControlStorePgSettings()
    return bool(s.pg_password and s.pg_password.get_secret_value())
