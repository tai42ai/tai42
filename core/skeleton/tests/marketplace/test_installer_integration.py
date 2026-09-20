"""A REAL Postgres exercise of the installer's plugin-migration step: an in-test plugin
declaring a schema chain has that chain APPLIED against a live Postgres through the store's
pooled connection — the table it declares actually appears and the ``tai_schema_history``
row is recorded — and a re-run is idempotent (roll-forward applies only the remainder).

The wiring/ordering is pinned without a database in ``test_installer_migrations.py`` (the
runner is faked); this exercises the real ``apply_migrations`` against Postgres so the chain
truly lands. It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres. Without the opt-in the tests SKIP VISIBLY
(never a silent skip)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString

import pytest
from psycopg import sql
from tai42_contract.plugins import PluginSpec
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.db.discovery as discovery
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
from tai42_skeleton.marketplace.installer import Installer

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


async def _drop_table(table: str) -> None:
    """Drop a run-created table by identifier — composed via ``psycopg.sql`` so the dynamic
    (hex-token) name is a bound identifier, not string-interpolated SQL."""
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))


async def _scalar(sql: LiteralString, params: tuple = ()) -> object:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(sql, params)
        row = await cur.fetchone()
    return None if row is None else row[0]


@dataclass
class _Plugin:
    installer: Installer
    spec: PluginSpec
    component: str
    table: str


@pytest.fixture
async def plugin(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Plugin]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres installer migration test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the real chain applied — no fake)"
        )
    reset_all_settings()
    await apply_migrations([skeleton_entry()])

    token = uuid.uuid4().hex[:12]
    import_pkg = f"tai42_layers_mig_{token}"
    dist_name = f"tai42-layers-mig-{token}"
    table = f"layers_plugin_{token}"

    # A minimal REAL importable package on ``sys.path`` carrying a well-formed one-file chain
    # (``NNNN_name.sql``) that creates a uniquely-named table.
    root = Path(tempfile.mkdtemp(prefix="tai42_layers_mig_"))
    package_dir = root / import_pkg
    (package_dir / "migrations").mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")
    (package_dir / "migrations" / "0001_baseline.sql").write_text(
        f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, note TEXT NOT NULL);\n"
    )
    sys.path.insert(0, str(root))
    # ``plugin_migration_entry`` resolves the distribution's import package from installed
    # metadata; this in-test package is not pip-installed, so map the name directly.
    monkeypatch.setattr(discovery, "_import_package_for_distribution", lambda distribution: import_pkg)

    spec = PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": f"mig-{token}",
            "package": dist_name,
            "version": "1.0.0",
            "description": "A table-owning integration-test plugin",
            "license": "Apache-2.0",
            "contract": ">=0.1,<1.0",
            "categories": ["dev"],
            "provides": [
                {"kind": "tool", "module": f"{import_pkg}.tools.x", "name": f"tool-{token}", "description": "A tool"}
            ],
            "migrations": "migrations",
        }
    )
    try:
        yield _Plugin(installer=Installer(), spec=spec, component=dist_name, table=table)
    finally:
        sys.path.remove(str(root))
        shutil.rmtree(root, ignore_errors=True)
        await _drop_table(table)
        await _exec("DELETE FROM tai_schema_history WHERE component = %s", (dist_name,))
        await shutdown_all_clients()


async def test_run_plugin_migrations_applies_chain_and_is_idempotent(plugin: _Plugin) -> None:
    p = plugin
    # The declared table does not exist before the chain runs.
    assert await _scalar("SELECT to_regclass(%s)", (p.table,)) is None

    await p.installer._run_plugin_migrations(p.spec)

    # The chain landed: the table exists and the history row is recorded under the plugin's
    # own component identity.
    assert await _scalar("SELECT to_regclass(%s)", (p.table,)) is not None
    recorded = await _scalar(
        "SELECT count(*) FROM tai_schema_history WHERE component = %s AND version = 1", (p.component,)
    )
    assert recorded == 1

    # A re-run is idempotent — the already-applied file rolls forward, leaving exactly one
    # history row and the one table.
    await p.installer._run_plugin_migrations(p.spec)
    still = await _scalar("SELECT count(*) FROM tai_schema_history WHERE component = %s", (p.component,))
    assert still == 1


async def test_plugin_without_chain_is_a_noop(plugin: _Plugin) -> None:
    p = plugin
    chainless = p.spec.model_copy(update={"migrations": None})
    # A plugin declaring no chain owns no tables — the opt-in step runs the real code path and
    # writes nothing (no table, no history row).
    await p.installer._run_plugin_migrations(chainless)
    assert await _scalar("SELECT to_regclass(%s)", (p.table,)) is None
    assert await _scalar("SELECT count(*) FROM tai_schema_history WHERE component = %s", (p.component,)) == 0
