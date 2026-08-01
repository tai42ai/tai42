"""The ``TAI_DEFAULT_*`` per-field fallback: a mapped connection-identity field
falls back to the shared default namespace when its own namespace leaves it
unset, specific always beats default, only identity fields are eligible, and the
default namespace logs exactly what it won (field names only, never values)."""

import logging
import os
from collections.abc import Mapping
from typing import ClassVar

import pytest
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from tai42_kit.clients.settings import PostgresConnectionSettings, RedisConnectionSettings
from tai42_kit.settings import DefaultNamespaceMixin, TaiBaseSettings, registered_settings
from tai42_kit.settings.registry import _clear_registry

_LOGGER_NAME = "tai42_kit.settings.default_namespace"

# Env prefixes any test may touch — stripped before each test so ambient env
# (or a leaked var) can never colour a resolution.
_TEST_ENV_PREFIXES = (
    "TAI_DEFAULT_",
    "PGSTORE_",
    "REDISSTORE_",
    "OUTER_",
    "BROKER_",
    "PG_",
    "REDIS_",
)


class PgStoreSettings(PostgresConnectionSettings):
    model_config = SettingsConfigDict(env_prefix="PGSTORE_")


class RedisStoreSettings(RedisConnectionSettings):
    model_config = SettingsConfigDict(env_prefix="REDISSTORE_")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Drop every connection/default env var so each test controls the whole set.

    monkeypatch restores them after the test, so no teardown of our own is needed."""
    for key in list(os.environ):
        if key.startswith(_TEST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the registry around every test so locally-defined classes don't leak."""
    _clear_registry()
    yield
    _clear_registry()


def _write_dotenv(path, **values: str) -> str:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    return str(path)


def test_no_default_namespace_resolves_to_todays_defaults():
    # With nothing set, a prefixed store resolves byte-for-byte to the canonical
    # class defaults — an active fallback map contributes nothing without matching env.
    assert PgStoreSettings().model_dump() == PostgresConnectionSettings().model_dump()
    assert RedisStoreSettings().model_dump() == RedisConnectionSettings().model_dump()


def test_pg_fields_fall_back_to_default_namespace(monkeypatch):
    # Host, an int (port), and a SecretStr (password) all fall back and coerce.
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-db")
    monkeypatch.setenv("TAI_DEFAULT_PG_PORT", "6001")
    monkeypatch.setenv("TAI_DEFAULT_PG_PASSWORD", "shared-secret")

    settings = PgStoreSettings()

    assert settings.pg_host == "shared-db"
    assert settings.pg_port == 6001  # coerced str -> int
    assert isinstance(settings.pg_password, SecretStr)
    assert settings.pg_password.get_secret_value() == "shared-secret"
    # An unmapped-by-env identity field keeps its class default.
    assert settings.pg_db == "postgres"


def test_redis_fields_fall_back_to_default_namespace(monkeypatch):
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    settings = RedisStoreSettings()

    assert settings.redis_url == "redis://shared:6379/0"
    assert settings.redis_max_connections == 42


def test_per_field_mixing_specific_and_default_in_one_instance(monkeypatch):
    # Specific namespace supplies pg_db; the default namespace fills host + creds,
    # all resolved in a single construction.
    monkeypatch.setenv("PGSTORE_PG_DB", "store-db")
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-db")
    monkeypatch.setenv("TAI_DEFAULT_PG_USER", "shared-user")
    monkeypatch.setenv("TAI_DEFAULT_PG_PASSWORD", "shared-secret")

    settings = PgStoreSettings()

    assert settings.pg_db == "store-db"  # specific wins
    assert settings.pg_host == "shared-db"
    assert settings.pg_user == "shared-user"
    assert settings.pg_password.get_secret_value() == "shared-secret"


def test_specific_env_beats_default_env(monkeypatch):
    monkeypatch.setenv("PGSTORE_PG_HOST", "store-host")
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-host")

    assert PgStoreSettings().pg_host == "store-host"


def test_specific_dotenv_beats_default_env(tmp_path, monkeypatch):
    dotenv = _write_dotenv(tmp_path / ".env", PGSTORE_PG_HOST="store-host")
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-host")

    class DotenvPgSettings(PostgresConnectionSettings):
        model_config = SettingsConfigDict(env_prefix="PGSTORE_", env_file=dotenv)

    assert DotenvPgSettings().pg_host == "store-host"


def test_init_kwarg_beats_everything(monkeypatch):
    monkeypatch.setenv("PGSTORE_PG_HOST", "store-host")
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-host")

    assert PgStoreSettings(pg_host="init-host").pg_host == "init-host"


def test_default_env_beats_default_dotenv(tmp_path, monkeypatch):
    dotenv = _write_dotenv(tmp_path / ".env", TAI_DEFAULT_PG_HOST="dotenv-host")
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "env-host")

    class DotenvPgSettings(PostgresConnectionSettings):
        model_config = SettingsConfigDict(env_prefix="PGSTORE_", env_file=dotenv)

    assert DotenvPgSettings().pg_host == "env-host"


def test_default_dotenv_resolves_alone(tmp_path):
    dotenv = _write_dotenv(tmp_path / ".env", TAI_DEFAULT_PG_HOST="dotenv-host")

    class DotenvPgSettings(PostgresConnectionSettings):
        model_config = SettingsConfigDict(env_prefix="PGSTORE_", env_file=dotenv)

    assert DotenvPgSettings().pg_host == "dotenv-host"


def test_empty_string_default_is_ignored(monkeypatch):
    # An empty-string TAI_DEFAULT_* value is skipped (env_ignore_empty parity), so
    # the field keeps its class default rather than becoming "".
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "")

    assert PgStoreSettings().pg_host == "localhost"


def test_non_identity_fields_never_fall_back(monkeypatch):
    # Behavior knobs are absent from the fallback maps, so their TAI_DEFAULT_*
    # forms are inert and the fields keep their class defaults.
    monkeypatch.setenv("TAI_DEFAULT_DECODE_RESPONSES", "false")
    monkeypatch.setenv("TAI_DEFAULT_SOCKET_TIMEOUT", "5")
    monkeypatch.setenv("TAI_DEFAULT_PG_MAX_CONNECTIONS", "99")

    redis = RedisStoreSettings()
    assert redis.decode_responses is True
    assert redis.socket_timeout is None

    assert PgStoreSettings().pg_max_connections == 10


def test_rename_mapping_resolves_from_shared_key(monkeypatch):
    # A field named differently from its default key (celery's broker_url shape)
    # still resolves from TAI_DEFAULT_REDIS_URL via the map.
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")

    class BrokerSettings(DefaultNamespaceMixin, TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="BROKER_")
        tai_default_fields: ClassVar[Mapping[str, str]] = {"broker_url": "redis_url"}
        broker_url: str | None = None

    assert BrokerSettings().broker_url == "redis://shared:6379/0"


def test_nested_composition_picks_up_default_namespace(monkeypatch):
    # A nested BaseSettings default_factory runs the nested class's own source
    # pipeline, so the default namespace reaches it through composition.
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-db")

    class OuterSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="OUTER_")
        pg: PgStoreSettings = Field(default_factory=PgStoreSettings)

    assert OuterSettings().pg.pg_host == "shared-db"


def test_provenance_log_lists_only_won_fields(monkeypatch, caplog):
    # pg_db is set specifically (must NOT be logged); host + password fall back to
    # the default (logged). One line, label = env_prefix, no values anywhere.
    monkeypatch.setenv("PGSTORE_PG_DB", "store-db")
    monkeypatch.setenv("TAI_DEFAULT_PG_HOST", "shared-db")
    monkeypatch.setenv("TAI_DEFAULT_PG_DB", "shared-db-name")
    monkeypatch.setenv("TAI_DEFAULT_PG_PASSWORD", "shared-secret")

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        PgStoreSettings()

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert message == "PGSTORE_: pg_host, pg_password ← TAI_DEFAULT_*"
    # A field the specific namespace won is absent, and no value ever appears.
    assert "pg_db" not in message
    assert "shared-db" not in message
    assert "shared-secret" not in message
    assert "store-db" not in message


def test_provenance_log_silent_when_nothing_falls_back(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        PgStoreSettings()

    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []


def test_registry_populates_default_namespace_var():
    class RegisteredPgSettings(PostgresConnectionSettings):
        model_config = SettingsConfigDict(env_prefix="PGSTORE_")

    fields = {f.name: f for f in next(i for i in registered_settings() if i.name == "RegisteredPgSettings").fields}
    # Mapped identity fields carry their TAI_DEFAULT_* var.
    assert fields["pg_host"].default_namespace_var == "TAI_DEFAULT_PG_HOST"
    assert fields["pg_password"].default_namespace_var == "TAI_DEFAULT_PG_PASSWORD"
    # A behavior knob on the same class carries none.
    assert fields["pg_max_connections"].default_namespace_var is None


def test_registry_none_for_class_without_mapping():
    class WidgetSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="WIDGET_")
        host: str = "localhost"

    group = next(i for i in registered_settings() if i.name == "WidgetSettings")
    field = next(f for f in group.fields if f.name == "host")
    assert field.default_namespace_var is None
