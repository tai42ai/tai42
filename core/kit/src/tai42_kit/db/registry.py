"""Central registry of named Postgres databases and component bindings.

A database is declared under ``TAI_DATABASE_<NAME>_`` (fields are the
``PostgresConnectionSettings`` field names, so ``TAI_DATABASE_DEFAULT_PG_HOST``
and friends). A database is CONFIGURED iff its password resolves non-empty.

Each database may carry a distinct admin (migrator) identity under
``TAI_DATABASE_<NAME>_PG_ADMIN_USER`` / ``_PG_ADMIN_PASSWORD``, both-or-neither:
both set is the admin identity; neither set migrates under the database's own
runtime user/password (the documented default, one identity for both). Exactly
one set is a loud error, never a silent pairing of one admin field with a
runtime one.

A migration COMPONENT binds to one database via ``TAI_DB_BINDING_<SLUG>`` (its
chain, guard, and every store share that database); unset binds to ``default``.
``<SLUG>`` is the component name uppercased with every char outside ``[A-Z0-9]``
replaced by ``_``.

Every function reads env FRESH per call (a fresh settings load under a per-name
prefix), so a config reload re-evaluates.
"""

import re
from functools import cache
from typing import ClassVar

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from tai42_kit.clients.settings import PostgresConnectionSettings
from tai42_kit.settings import TaiBaseSettings

_DEFAULT_BINDING = "default"


class DatabaseNotConfiguredError(Exception):
    """A named database is used but its password is unset or empty.

    Names the database and the env var that turns it on; never a silent
    passwordless or localhost connection.
    """


class AdminIdentityIncompleteError(Exception):
    """A database's admin (migrator) identity is half-set.

    The admin user and password are both-or-neither; exactly one set names both
    env vars and the rule, never silently pairs the admin user with the runtime
    password.
    """


class _AdminIdentity(TaiBaseSettings):
    # Per-database admin (migrator) identity, loaded under the database's own
    # ``TAI_DATABASE_<NAME>_`` prefix so the fields resolve to
    # ``TAI_DATABASE_<NAME>_PG_ADMIN_USER`` / ``_PG_ADMIN_PASSWORD``, both-or-
    # neither: both unset migrates the runtime identity, exactly one set is a loud
    # error. Abstract base — a per-name subclass fixes the prefix; excluded so the
    # prefixless base is not a bogus group.
    registry_exclude: ClassVar[bool] = True

    pg_admin_user: str | None = None
    pg_admin_password: SecretStr | None = None


class _ComponentBinding(TaiBaseSettings):
    # Abstract base for a per-component binding read. A subclass fixes the env var
    # via ``value``'s ``validation_alias``; excluded so the aliasless base is not a
    # bogus group.
    registry_exclude: ClassVar[bool] = True

    value: str = _DEFAULT_BINDING


def _database_prefix(name: str) -> str:
    return f"TAI_DATABASE_{name.upper()}_"


def _component_slug(component: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", component.upper())


def _binding_env(component: str) -> str:
    return f"TAI_DB_BINDING_{_component_slug(component)}"


@cache
def _database_settings_cls(prefix: str) -> type[PostgresConnectionSettings]:
    # One cached settings class per database prefix; each instantiation reads env
    # fresh. ``env_prefix`` on the class (not an init override) so ``pg_dsn`` names
    # the exact ``TAI_DATABASE_<NAME>_PG_*`` var in its unconfigured error.
    class _Database(PostgresConnectionSettings):
        registry_exclude: ClassVar[bool] = True

        model_config = SettingsConfigDict(env_prefix=prefix)

    return _Database


@cache
def _admin_identity_cls(prefix: str) -> type[_AdminIdentity]:
    class _Admin(_AdminIdentity):
        registry_exclude: ClassVar[bool] = True

        model_config = SettingsConfigDict(env_prefix=prefix)

    return _Admin


@cache
def _binding_settings_cls(env_var: str) -> type[_ComponentBinding]:
    # One cached settings class per binding env var so the read goes through the
    # settings machinery (env + ``.env``, case-insensitive) rather than a raw
    # ``os.environ`` peek. The class is cached; each instantiation reads fresh.
    class _Binding(_ComponentBinding):
        registry_exclude: ClassVar[bool] = True

        value: str = Field(default=_DEFAULT_BINDING, validation_alias=env_var)

    return _Binding


class _DeclaredBinding(TaiBaseSettings):
    # Abstract base for a per-component binding read that distinguishes SET from
    # default: ``value`` has no default, so an unset env var resolves to None. A
    # subclass fixes the env var via ``value``'s ``validation_alias``; excluded so
    # the aliasless base is not a bogus group.
    registry_exclude: ClassVar[bool] = True

    value: str | None = None


@cache
def _declared_binding_settings_cls(env_var: str) -> type[_DeclaredBinding]:
    # One cached settings class per binding env var; each instantiation reads env
    # fresh. Unset resolves to None so set-vs-default is observable.
    class _Declared(_DeclaredBinding):
        registry_exclude: ClassVar[bool] = True

        value: str | None = Field(default=None, validation_alias=env_var)

    return _Declared


def _database(name: str) -> PostgresConnectionSettings:
    return _database_settings_cls(_database_prefix(name))()


def _is_configured(settings: PostgresConnectionSettings) -> bool:
    password = settings.pg_password
    return password is not None and password.get_secret_value() != ""


def database_password_env(name: str) -> str:
    """The env var whose non-empty value turns the named database on.

    The same var :class:`DatabaseNotConfiguredError` points at.
    """
    return f"{_database_prefix(name)}PG_PASSWORD"


def database_configured(name: str) -> bool:
    """Whether the named database resolves a non-empty password."""
    return _is_configured(_database(name))


def database_settings(name: str) -> PostgresConnectionSettings:
    """The named database's runtime connection settings.

    Raises :class:`DatabaseNotConfiguredError` when the database is unconfigured
    (no non-empty password).
    """
    settings = _database(name)
    if not _is_configured(settings):
        raise DatabaseNotConfiguredError(f"database {name!r} is not configured: set {database_password_env(name)}.")
    return settings


def admin_database_settings(name: str) -> PostgresConnectionSettings:
    """The named database's admin (migrator) connection settings.

    Same host/port/db/pool as the runtime settings. The per-database admin
    identity is both-or-neither: both fields set overrides user/password; neither
    set migrates under the runtime identity. Exactly one set raises
    :class:`AdminIdentityIncompleteError`, never silently pairs the admin user with
    the runtime password. Raises :class:`DatabaseNotConfiguredError` when the
    database is unconfigured.
    """
    runtime = database_settings(name)
    prefix = _database_prefix(name)
    admin = _admin_identity_cls(prefix)()
    user_set = admin.pg_admin_user is not None
    password_set = admin.pg_admin_password is not None
    if user_set != password_set:
        raise AdminIdentityIncompleteError(
            f"database {name!r} has a half-set admin identity: set BOTH "
            f"{prefix}PG_ADMIN_USER and {prefix}PG_ADMIN_PASSWORD, or neither."
        )
    if not user_set:
        return runtime
    return runtime.model_copy(update={"pg_user": admin.pg_admin_user, "pg_password": admin.pg_admin_password})


def component_binding(component: str) -> str:
    """The database name a migration component binds to (default ``"default"``)."""
    return _binding_settings_cls(_binding_env(component))().value


def component_binding_declared(component: str) -> str | None:
    """The database name a migration component binds to, only when EXPLICITLY set.

    The literal value of ``TAI_DB_BINDING_<SLUG>`` when set (including ``"default"``);
    ``None`` when unset. Exposes set-vs-default, which :func:`component_binding`'s
    ``"default"`` fallback cannot.
    """
    return _declared_binding_settings_cls(_binding_env(component))().value


def component_store_settings(component: str) -> PostgresConnectionSettings:
    """The runtime settings of the database the component binds to."""
    return database_settings(component_binding(component))


def component_store_configured(component: str) -> bool:
    """Whether the database the component binds to is configured."""
    return database_configured(component_binding(component))


def component_migrator_settings(component: str) -> PostgresConnectionSettings:
    """The admin (migrator) settings of the database the component binds to."""
    return admin_database_settings(component_binding(component))
