"""The named-database registry: database configuration/resolution, the per-
database admin identity with its runtime fallback, and component bindings.

Every function serves the settings resolved once per process, re-read after a
settings reset or a change of the env file's identity; the suite resets settings
around each test, so tests set env with monkeypatch and assert on the resolved
settings without touching a real Postgres.
"""

import os
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from tai42_kit.clients.settings import PostgresConnectionSettings
from tai42_kit.db import (
    AdminIdentityIncompleteError,
    DatabaseNotConfiguredError,
    admin_database_settings,
    component_binding,
    component_migrator_settings,
    component_store_configured,
    component_store_settings,
    database_configured,
    database_password_env,
    database_settings,
)
from tai42_kit.settings import reset_all_settings

# Prefixes any test may set — stripped before each test so ambient env cannot
# colour a resolution.
_TEST_ENV_PREFIXES = ("TAI_DATABASE_", "TAI_DB_BINDING_")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(_TEST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


def _replace_env_file(directory: Path, text: str) -> None:
    staged = directory / ".env.staged"
    staged.write_text(text)
    os.replace(staged, directory / ".env")


@pytest.fixture
def constructions(monkeypatch) -> list[str]:
    """Record the class name of every settings construction."""
    built: list[str] = []
    original = BaseSettings.__init__

    def _counting(self, *args, **kwargs):
        built.append(type(self).__name__)
        original(self, *args, **kwargs)

    monkeypatch.setattr(BaseSettings, "__init__", _counting)
    return built


class TestResolvedOncePerProcess:
    def test_a_second_component_store_settings_call_constructs_nothing(self, monkeypatch, constructions):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")

        first = component_store_settings("skeleton")
        built_by_first = len(constructions)
        second = component_store_settings("skeleton")

        assert second is first
        assert built_by_first == 2
        assert len(constructions) == built_by_first

    def test_admin_database_settings_construct_once(self, monkeypatch, constructions):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_USER", "migrator")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD", "migrator-pw")

        first = admin_database_settings("default")
        built = len(constructions)
        second = admin_database_settings("default")

        assert len(constructions) == built
        assert second == first
        assert second.pg_user == "migrator"

    def test_a_settings_reset_re_reads(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "first-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")
        assert database_settings("default").pg_host == "first-host"

        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "second-host")
        assert database_settings("default").pg_host == "first-host"

        reset_all_settings()
        assert database_settings("default").pg_host == "second-host"

    def test_a_replaced_env_file_re_reads(self, tmp_path):
        (tmp_path / ".env").write_text("TAI_DATABASE_DEFAULT_PG_HOST=first-host\nTAI_DATABASE_DEFAULT_PG_PASSWORD=pw\n")
        assert database_settings("default").pg_host == "first-host"

        _replace_env_file(tmp_path, "TAI_DATABASE_DEFAULT_PG_HOST=second-host\nTAI_DATABASE_DEFAULT_PG_PASSWORD=pw\n")

        assert database_settings("default").pg_host == "second-host"

    def test_an_unconfigured_database_raises_on_every_call(self, monkeypatch, constructions):
        for _ in range(3):
            with pytest.raises(DatabaseNotConfiguredError):
                database_settings("default")
        # The resolved (unconfigured) settings are served once; the check runs per call.
        assert constructions.count("_Database") == 1

        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")
        reset_all_settings()
        assert database_configured("default") is True

    def test_a_half_set_admin_identity_raises_on_every_call(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "runtime-pw")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_USER", "migrator")

        for _ in range(3):
            with pytest.raises(AdminIdentityIncompleteError):
                admin_database_settings("default")


class TestDatabasePasswordEnv:
    def test_names_the_uppercased_password_var(self):
        assert database_password_env("default") == "TAI_DATABASE_DEFAULT_PG_PASSWORD"

    def test_uppercases_the_name(self):
        assert database_password_env("analytics") == "TAI_DATABASE_ANALYTICS_PG_PASSWORD"


class TestComponentBinding:
    def test_default_when_unset(self):
        assert component_binding("skeleton") == "default"

    def test_reads_the_slugged_binding_var(self, monkeypatch):
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "analytics")
        assert component_binding("skeleton") == "analytics"

    def test_slug_replaces_non_alnum_and_uppercases(self, monkeypatch):
        # A distribution name with hyphens slugs to underscores, uppercased.
        monkeypatch.setenv("TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES", "accounts")
        assert component_binding("tai42-accounts-postgres") == "accounts"

    def test_re_read_after_a_settings_reset(self, monkeypatch):
        assert component_binding("skeleton") == "default"
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "warehouse")
        assert component_binding("skeleton") == "default"
        reset_all_settings()
        assert component_binding("skeleton") == "warehouse"

    def test_re_read_after_a_rewrite_of_the_env_file(self, tmp_path):
        (tmp_path / ".env").write_text("TAI_DB_BINDING_SKELETON=warehouse\n")
        assert component_binding("skeleton") == "warehouse"
        _replace_env_file(tmp_path, "TAI_DB_BINDING_SKELETON=analytics\n")
        assert component_binding("skeleton") == "analytics"


class TestDatabaseConfigured:
    def test_false_when_password_unset(self):
        assert database_configured("default") is False

    def test_false_when_password_empty(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "")
        assert database_configured("default") is False

    def test_true_when_password_set(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")
        assert database_configured("default") is True

    def test_scoped_per_name(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_ANALYTICS_PG_PASSWORD", "s3cr3t")
        assert database_configured("analytics") is True
        assert database_configured("default") is False


class TestDatabaseSettings:
    def test_resolves_fields_under_the_database_prefix(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PORT", "6001")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_DB", "app")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_USER", "runtime")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")

        settings = database_settings("default")

        assert isinstance(settings, PostgresConnectionSettings)
        assert settings.pg_host == "db-host"
        assert settings.pg_port == 6001
        assert settings.pg_db == "app"
        assert settings.pg_user == "runtime"
        assert settings.pg_password is not None
        assert settings.pg_password.get_secret_value() == "s3cr3t"

    def test_raises_named_error_when_unconfigured(self):
        with pytest.raises(
            DatabaseNotConfiguredError,
            match=r"database 'default' is not configured: set TAI_DATABASE_DEFAULT_PG_PASSWORD\.",
        ):
            database_settings("default")

    def test_named_error_uses_the_database_name(self, monkeypatch):
        with pytest.raises(
            DatabaseNotConfiguredError,
            match=r"database 'analytics' is not configured: set TAI_DATABASE_ANALYTICS_PG_PASSWORD\.",
        ):
            database_settings("analytics")


class TestAdminDatabaseSettings:
    def test_falls_back_to_runtime_identity_when_admin_unset(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_USER", "runtime")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "runtime-pw")

        admin = admin_database_settings("default")

        # No distinct admin identity: one identity migrates and runs.
        assert admin.pg_user == "runtime"
        assert admin.pg_password is not None
        assert admin.pg_password.get_secret_value() == "runtime-pw"
        assert admin.pg_host == "db-host"

    def test_admin_identity_overrides_runtime(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PORT", "6001")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_DB", "app")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_USER", "runtime")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "runtime-pw")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_USER", "migrator")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD", "migrator-pw")

        admin = admin_database_settings("default")

        # User/password come from the admin identity; everything else stays the
        # database's runtime connection.
        assert admin.pg_user == "migrator"
        assert admin.pg_password is not None
        assert admin.pg_password.get_secret_value() == "migrator-pw"
        assert admin.pg_host == "db-host"
        assert admin.pg_port == 6001
        assert admin.pg_db == "app"

    def test_admin_user_only_raises(self, monkeypatch):
        # Admin user set, admin password unset: half-set is a loud error naming both
        # env vars and the both-or-neither rule, never a silent runtime-password pair.
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_USER", "runtime")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "runtime-pw")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_USER", "migrator")

        with pytest.raises(
            AdminIdentityIncompleteError,
            match=(
                r"database 'default' has a half-set admin identity: set BOTH "
                r"TAI_DATABASE_DEFAULT_PG_ADMIN_USER and TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD, or neither\."
            ),
        ):
            admin_database_settings("default")

    def test_admin_password_only_raises(self, monkeypatch):
        # Admin password set, admin user unset: the mirror half-set — also a loud error.
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_USER", "runtime")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "runtime-pw")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD", "migrator-pw")

        with pytest.raises(
            AdminIdentityIncompleteError,
            match=(
                r"database 'default' has a half-set admin identity: set BOTH "
                r"TAI_DATABASE_DEFAULT_PG_ADMIN_USER and TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD, or neither\."
            ),
        ):
            admin_database_settings("default")

    def test_raises_named_error_when_database_unconfigured(self):
        with pytest.raises(DatabaseNotConfiguredError, match=r"database 'default' is not configured"):
            admin_database_settings("default")


class TestComponentResolution:
    def test_store_settings_follow_the_binding(self, monkeypatch):
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "warehouse")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_HOST", "wh-host")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_PASSWORD", "wh-pw")

        settings = component_store_settings("skeleton")

        assert settings.pg_host == "wh-host"

    def test_store_configured_follows_the_binding(self, monkeypatch):
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "warehouse")
        assert component_store_configured("skeleton") is False
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_PASSWORD", "wh-pw")
        reset_all_settings()
        assert component_store_configured("skeleton") is True

    def test_default_binding_when_unset(self, monkeypatch):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "db-host")
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "s3cr3t")

        assert component_store_settings("skeleton").pg_host == "db-host"
        assert component_store_configured("skeleton") is True

    def test_migrator_settings_use_the_bound_admin_identity(self, monkeypatch):
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "warehouse")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_HOST", "wh-host")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_USER", "runtime")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_PASSWORD", "runtime-pw")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_ADMIN_USER", "migrator")
        monkeypatch.setenv("TAI_DATABASE_WAREHOUSE_PG_ADMIN_PASSWORD", "migrator-pw")

        admin = component_migrator_settings("skeleton")

        assert admin.pg_host == "wh-host"
        assert admin.pg_user == "migrator"
        assert admin.pg_password is not None
        assert admin.pg_password.get_secret_value() == "migrator-pw"

    def test_migrator_raises_when_bound_database_unconfigured(self, monkeypatch):
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "warehouse")
        with pytest.raises(DatabaseNotConfiguredError, match=r"database 'warehouse' is not configured"):
            component_migrator_settings("skeleton")
