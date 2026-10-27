"""The startup schema check gates the accounts-postgres chain when its module is loaded.

The real accounts-postgres distribution installed in the environment declares a chain in
its packaged spec. On a database where the skeleton-owned chains are applied and the
accounts chain is not, the schema check every epoch build runs refuses a manifest that
loads the accounts provider, naming the pending accounts chain and ``tai db migrate``; once
the installed plugin chains are applied, the same manifest passes the check.

Needs only the shared Postgres, not a booted stack: the leg owns a scratch database it drops
at teardown, and the registry's ``TAI_DATABASE_DEFAULT_*`` coordinates point at it.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
import tai42_accounts_postgres
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.db import SchemaOutOfDateError, apply_migrations
from tai42_kit.settings import reset_all_settings
from tai42_skeleton.db import assert_deployment_schemas_applied, installed_plugin_entries, skeleton_entry
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import distribution_map
from tai42_skeleton.states.db import states_entry

from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.needs("no-stack", "store:postgres")

_ACCOUNTS_DISTRIBUTION = "tai42-accounts-postgres"
_ACCOUNTS_IMPORT_PACKAGE = "tai42_accounts_postgres"


@pytest.fixture(scope="module")
def harness_settings() -> HarnessSettings:
    return HarnessSettings()


@pytest.fixture(scope="module")
def pg_admin(harness_settings: HarnessSettings) -> PostgresAdmin:
    admin = PostgresAdmin(harness_settings)
    admin.check_reachable()
    return admin


@pytest.fixture
def scratch_store(
    monkeypatch: pytest.MonkeyPatch, harness_settings: HarnessSettings, pg_admin: PostgresAdmin
) -> Iterator[None]:
    """A fresh, EMPTY database the default-database registry coordinates point at, dropped at teardown."""
    name = f"tai42_e2e_accgate_{secrets.token_hex(4)}"
    pg_admin.create_empty_db(name)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", harness_settings.pg_host)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PORT", str(harness_settings.pg_port))
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_DB", name)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_USER", harness_settings.pg_user)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", harness_settings.pg_password)
    monkeypatch.delenv("TAI_PLUGINS_PREFIX", raising=False)
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()
        pg_admin.drop_stack_db(name)


def _manifest_loading_accounts() -> Manifest:
    return Manifest.model_validate({"default_routers": "none", "lifecycle_modules": [_ACCOUNTS_IMPORT_PACKAGE]})


async def test_the_schema_check_gates_the_accounts_chain_of_a_loaded_provider(scratch_store: None) -> None:
    manifest = _manifest_loading_accounts()
    accounts_entry = next(entry for entry in installed_plugin_entries() if entry.component == _ACCOUNTS_DISTRIBUTION)
    assert Path(str(accounts_entry.migrations_dir)) == Path(tai42_accounts_postgres.__file__).parent / "migrations"
    try:
        await apply_migrations([skeleton_entry(), states_entry()])

        with pytest.raises(SchemaOutOfDateError) as refused:
            await assert_deployment_schemas_applied(manifest, distribution_map())

        message = str(refused.value)
        assert f"{_ACCOUNTS_DISTRIBUTION}: 1 pending (0001_baseline.sql)" in message
        assert "tai db migrate" in message
        assert "skeleton:" not in message
        assert "states:" not in message

        await apply_migrations(installed_plugin_entries())
        await assert_deployment_schemas_applied(manifest, distribution_map())
    finally:
        await shutdown_all_clients()
