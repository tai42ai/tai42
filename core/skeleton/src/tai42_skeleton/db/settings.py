"""The DDL-privileged migrator identity — the ``TAI_DB_*`` connection the
migration runner applies chains under.

A dedicated schema-admin namespace (falling back to ``TAI_DEFAULT_*`` like every
other kit connection): a deployment points it at the same Postgres its runtime
stores use but can give it a schema-owning role distinct from the app's
lesser-privileged runtime roles. ``tai db migrate``/``status`` and the marketplace
installer's plugin-migration step all run under this identity.
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.settings import settings_cache


class SchemaAdminSettings(PostgresConnectionSettings):
    """``TAI_DB_*`` Postgres connection for the schema migrator. No baked-in
    credential — supply the password via ``TAI_DB_PG_PASSWORD`` (or the shared
    ``TAI_DEFAULT_PG_PASSWORD``)."""

    model_config = SettingsConfigDict(env_prefix="TAI_DB_")

    pg_db: str = "tai"


@settings_cache
def schema_settings() -> SchemaAdminSettings:
    return SchemaAdminSettings()
