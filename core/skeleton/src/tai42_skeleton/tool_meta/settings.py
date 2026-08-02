"""Pydantic-settings for the tool-metadata store's Postgres connection.

The overlay store is a platform primitive with its own ``TOOL_META_STORE_*``
namespace, kept separate from the versioning / connector / access-control stores
so each durable store declares its own DSN. It targets the same ``tai`` database
by default; a deployment points every store at its Postgres via the shared env.
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.settings import settings_cache


class ToolMetaStorePgSettings(PostgresConnectionSettings):
    """``TOOL_META_STORE_*`` Postgres connection for ``tool_folders`` +
    ``tool_meta``. No baked-in credential — supply the password via
    ``TOOL_META_STORE_PG_PASSWORD``."""

    model_config = SettingsConfigDict(env_prefix="TOOL_META_STORE_")

    pg_db: str = "tai"


@settings_cache
def tool_meta_store_settings() -> ToolMetaStorePgSettings:
    return ToolMetaStorePgSettings()


def tool_meta_store_configured() -> bool:
    """Whether this deployment configures the tool-metadata Postgres store at all.

    Resolved through the SAME pydantic-settings the store connects with (its own
    ``TOOL_META_STORE_*`` env or the shared ``TAI_DEFAULT_PG_PASSWORD``), read
    fresh — not the cached singleton — so a config reload re-evaluates. The store
    carries no baked-in credential, so a supplied password is the signal a real
    store is wired up; without one the overlay reads answer empty and its writes
    refuse rather than reaching for an absent Postgres."""
    s = ToolMetaStorePgSettings()
    return bool(s.pg_password and s.pg_password.get_secret_value())
