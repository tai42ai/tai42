"""The ``TAI_DEFAULT_*`` per-field fallback: a mapped connection-identity field
falls back to the shared default namespace when its own namespace leaves it
unset, specific always beats default, only mapped fields are eligible, and the
default namespace logs exactly what it won (field names only, never values).

Redis is the real remaining consumer of the fallback; a neutral synthetic
``StoreSettings`` exercises the machinery's coercion (str/int/SecretStr), mixing,
dotenv ordering, and provenance without tying it to any one product class."""

import logging
import os
from collections.abc import Mapping
from typing import ClassVar

import pytest
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from tai42_kit.clients.settings import RedisConnectionSettings
from tai42_kit.settings import DefaultNamespaceMixin, TaiBaseSettings, registered_settings
from tai42_kit.settings.registry import _clear_registry

_LOGGER_NAME = "tai42_kit.settings.default_namespace"

# Env prefixes any test may touch — stripped before each test so ambient env
# (or a leaked var) can never colour a resolution.
_TEST_ENV_PREFIXES = (
    "TAI_DEFAULT_",
    "STORE_",
    "REDISSTORE_",
    "OUTER_",
    "BROKER_",
    "REDIS_",
)


class StoreSettings(DefaultNamespaceMixin, TaiBaseSettings):
    """Synthetic store with the field shapes the machinery must handle: a string,
    an int, and a SecretStr mapped to the shared namespace, plus a mapped-but-
    absent identity field and an unmapped behavior knob."""

    model_config = SettingsConfigDict(env_prefix="STORE_")
    tai_default_fields: ClassVar[Mapping[str, str]] = {
        "host": "host",
        "port": "port",
        "secret": "secret",
        "label": "label",
    }

    host: str | None = None
    port: int = 5432
    secret: SecretStr | None = None
    label: str = "app"
    pool_size: int = 10


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
    # With nothing set, a store resolves byte-for-byte to its class defaults — an
    # active fallback map contributes nothing without matching env.
    assert StoreSettings().model_dump() == {
        "host": None,
        "port": 5432,
        "secret": None,
        "label": "app",
        "pool_size": 10,
    }
    assert RedisStoreSettings().model_dump() == RedisConnectionSettings().model_dump()


def test_mapped_fields_fall_back_to_default_namespace(monkeypatch):
    # A string, an int (port), and a SecretStr all fall back and coerce.
    monkeypatch.setenv("TAI_DEFAULT_HOST", "shared-db")
    monkeypatch.setenv("TAI_DEFAULT_PORT", "6001")
    monkeypatch.setenv("TAI_DEFAULT_SECRET", "shared-secret")

    settings = StoreSettings()

    assert settings.host == "shared-db"
    assert settings.port == 6001  # coerced str -> int
    assert isinstance(settings.secret, SecretStr)
    assert settings.secret.get_secret_value() == "shared-secret"
    # A mapped identity field with no matching env keeps its class default.
    assert settings.label == "app"


def test_redis_fields_fall_back_to_default_namespace(monkeypatch):
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_MAX_CONNECTIONS", "42")

    settings = RedisStoreSettings()

    assert settings.redis_url == "redis://shared:6379/0"
    assert settings.redis_max_connections == 42


def test_per_field_mixing_specific_and_default_in_one_instance(monkeypatch):
    # Specific namespace supplies host; the default namespace fills port + secret,
    # all resolved in a single construction.
    monkeypatch.setenv("STORE_HOST", "store-host")
    monkeypatch.setenv("TAI_DEFAULT_PORT", "6001")
    monkeypatch.setenv("TAI_DEFAULT_SECRET", "shared-secret")

    settings = StoreSettings()

    assert settings.host == "store-host"  # specific wins
    assert settings.port == 6001
    assert isinstance(settings.secret, SecretStr)
    assert settings.secret.get_secret_value() == "shared-secret"


def test_specific_env_beats_default_env(monkeypatch):
    monkeypatch.setenv("STORE_HOST", "store-host")
    monkeypatch.setenv("TAI_DEFAULT_HOST", "shared-host")

    assert StoreSettings().host == "store-host"


def test_specific_dotenv_beats_default_env(tmp_path, monkeypatch):
    dotenv = _write_dotenv(tmp_path / ".env", STORE_HOST="store-host")
    monkeypatch.setenv("TAI_DEFAULT_HOST", "shared-host")

    class DotenvStoreSettings(StoreSettings):
        model_config = SettingsConfigDict(env_prefix="STORE_", env_file=dotenv)

    assert DotenvStoreSettings().host == "store-host"


def test_init_kwarg_beats_everything(monkeypatch):
    monkeypatch.setenv("STORE_HOST", "store-host")
    monkeypatch.setenv("TAI_DEFAULT_HOST", "shared-host")

    assert StoreSettings(host="init-host").host == "init-host"


def test_default_env_beats_default_dotenv(tmp_path, monkeypatch):
    dotenv = _write_dotenv(tmp_path / ".env", TAI_DEFAULT_HOST="dotenv-host")
    monkeypatch.setenv("TAI_DEFAULT_HOST", "env-host")

    class DotenvStoreSettings(StoreSettings):
        model_config = SettingsConfigDict(env_prefix="STORE_", env_file=dotenv)

    assert DotenvStoreSettings().host == "env-host"


def test_default_dotenv_resolves_alone(tmp_path):
    dotenv = _write_dotenv(tmp_path / ".env", TAI_DEFAULT_HOST="dotenv-host")

    class DotenvStoreSettings(StoreSettings):
        model_config = SettingsConfigDict(env_prefix="STORE_", env_file=dotenv)

    assert DotenvStoreSettings().host == "dotenv-host"


def test_empty_string_default_is_ignored(monkeypatch):
    # An empty-string TAI_DEFAULT_* value is skipped (env_ignore_empty parity), so
    # the field keeps its class default (None) rather than becoming "".
    monkeypatch.setenv("TAI_DEFAULT_HOST", "")

    assert StoreSettings().host is None


def test_non_mapped_fields_never_fall_back(monkeypatch):
    # Behavior knobs are absent from the fallback maps, so their TAI_DEFAULT_*
    # forms are inert and the fields keep their class defaults.
    monkeypatch.setenv("TAI_DEFAULT_DECODE_RESPONSES", "false")
    monkeypatch.setenv("TAI_DEFAULT_SOCKET_TIMEOUT", "5")
    monkeypatch.setenv("TAI_DEFAULT_POOL_SIZE", "99")

    redis = RedisStoreSettings()
    assert redis.decode_responses is True
    assert redis.socket_timeout is None

    assert StoreSettings().pool_size == 10


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
    monkeypatch.setenv("TAI_DEFAULT_HOST", "shared-db")

    class OuterSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="OUTER_")
        store: StoreSettings = Field(default_factory=StoreSettings)

    assert OuterSettings().store.host == "shared-db"


def test_provenance_log_lists_only_won_fields(monkeypatch, caplog):
    # host is set specifically (must NOT be logged); port + secret fall back to
    # the default (logged). One line, label = env_prefix, no values anywhere.
    monkeypatch.setenv("STORE_HOST", "store-host")
    monkeypatch.setenv("TAI_DEFAULT_HOST", "shared-host")
    monkeypatch.setenv("TAI_DEFAULT_PORT", "6001")
    monkeypatch.setenv("TAI_DEFAULT_SECRET", "shared-secret")

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        StoreSettings()

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert message == "STORE_: port, secret ← TAI_DEFAULT_*"
    # A field the specific namespace won is absent, and no value ever appears.
    assert "host" not in message
    assert "store-host" not in message
    assert "shared-secret" not in message


def test_provenance_log_silent_when_nothing_falls_back(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        StoreSettings()

    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []


def test_registry_populates_default_namespace_var():
    class RegisteredStoreSettings(StoreSettings):
        model_config = SettingsConfigDict(env_prefix="STORE_")

    fields = {f.name: f for f in next(i for i in registered_settings() if i.name == "RegisteredStoreSettings").fields}
    # Mapped fields carry their TAI_DEFAULT_* var.
    assert fields["host"].default_namespace_var == "TAI_DEFAULT_HOST"
    assert fields["secret"].default_namespace_var == "TAI_DEFAULT_SECRET"
    # A behavior knob on the same class carries none.
    assert fields["pool_size"].default_namespace_var is None


def test_registry_none_for_class_without_mapping():
    class WidgetSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="WIDGET_")
        host: str = "localhost"

    group = next(i for i in registered_settings() if i.name == "WidgetSettings")
    field = next(f for f in group.fields if f.name == "host")
    assert field.default_namespace_var is None
