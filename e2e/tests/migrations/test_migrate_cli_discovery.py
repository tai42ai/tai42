"""``tai db migrate`` chain discovery, driven through the REAL CLI over a
subprocess against a live Postgres — the composed path an operator runs.

Two legs pin the CLI's own discovery where a hand-built entry list cannot:

- *fresh database*: a truly empty database (no tables at all, no manifest)
  migrates cleanly — the skeleton-owned chains and the chain of every plugin
  installed in the environment (accounts-postgres among them) land in one run,
  with no traceback.
- *prefix-preinstalled plugins*: a neutral chain-declaring plugin laid into the
  plugins prefix the way ``pip install --prefix`` leaves it, with NO marketplace
  install row, has its chain applied in the same run; a second copy of
  accounts-postgres in the prefix (shadowed by the environment copy) still gives
  exactly one entry for that distribution, and a second run is an inert no-op.

These need only the shared Postgres, not a booted stack: each leg owns a scratch
database it drops at teardown. The subprocess env carries only the registry's
``TAI_DATABASE_DEFAULT_*`` coordinates (plus ``TAI_PLUGINS_PREFIX`` on the prefix
leg), so the CLI resolves its connection exactly as a deployment does.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from tai42_skeleton.marketplace.prefix import prefix_site_dirs

from tai42_e2e.binaries import tai_bin
from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.needs("no-stack", "cli", "store:postgres")

_ACCOUNTS_DISTRIBUTION = "tai42-accounts-postgres"
_ACCOUNTS_IMPORT_PACKAGE = "tai42_accounts_postgres"

_PROBE_DISTRIBUTION = "tai42-prefixprobe"
_PROBE_IMPORT_PACKAGE = "tai42_prefixprobe"
_PROBE_TABLE = "prefixprobe_items"
_PROBE_SPEC = f"""\
spec_version: 1
namespace: tai42
name: prefixprobe
package: {_PROBE_DISTRIBUTION}
version: 1.0.0
description: A chain-declaring test plugin
license: Apache-2.0
contract: '>=0.1,<1000'
categories: [dev]
provides:
  - kind: tool
    name: prefixprobe_ping
    module: {_PROBE_IMPORT_PACKAGE}.tools
    description: Answer a fixed reply
migrations: migrations
"""


@pytest.fixture(scope="module")
def harness_settings() -> HarnessSettings:
    return HarnessSettings()


@pytest.fixture(scope="module")
def pg_admin(harness_settings: HarnessSettings) -> PostgresAdmin:
    admin = PostgresAdmin(harness_settings)
    admin.check_reachable()
    return admin


@pytest.fixture
def scratch_db(pg_admin: PostgresAdmin) -> Iterator[str]:
    """A fresh, EMPTY database (no tables at all) dropped at teardown."""
    name = f"tai42_e2e_clidisc_{secrets.token_hex(4)}"
    pg_admin.create_empty_db(name)
    try:
        yield name
    finally:
        pg_admin.drop_stack_db(name)


def _migrate_env(harness: HarnessSettings, dbname: str, *, prefix: Path | None = None) -> dict[str, str]:
    """The clean child env an operator's ``tai db migrate`` runs under: the
    registry's default-database coordinates pointing at the scratch database, and
    the plugins prefix when the leg opts into one."""
    venv_bin = str(Path(sys.executable).parent)
    env = {
        "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TAI_DATABASE_DEFAULT_PG_HOST": harness.pg_host,
        "TAI_DATABASE_DEFAULT_PG_PORT": str(harness.pg_port),
        "TAI_DATABASE_DEFAULT_PG_DB": dbname,
        "TAI_DATABASE_DEFAULT_PG_USER": harness.pg_user,
        "TAI_DATABASE_DEFAULT_PG_PASSWORD": harness.pg_password,
    }
    if prefix is not None:
        env["TAI_PLUGINS_PREFIX"] = str(prefix)
    return env


def _run_migrate(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the REAL ``tai db migrate`` console script — the operator's door."""
    return subprocess.run(
        [tai_bin(), "db", "migrate"], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=180
    )


def _connect(harness: HarnessSettings, dbname: str) -> psycopg.Connection:
    return psycopg.connect(
        host=harness.pg_host,
        port=harness.pg_port,
        user=harness.pg_user,
        password=harness.pg_password,
        dbname=dbname,
    )


def _public_tables(harness: HarnessSettings, dbname: str) -> set[str]:
    with _connect(harness, dbname) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {row[0] for row in cur.fetchall()}


def _history_components(harness: HarnessSettings, dbname: str) -> set[str]:
    with _connect(harness, dbname) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT component FROM tai_schema_history")
        return {row[0] for row in cur.fetchall()}


def _write_distribution(site: Path, dist_name: str, version: str, package: Path) -> None:
    """Write the ``.dist-info`` ``pip install`` leaves beside ``package``: its METADATA and a
    ``RECORD`` listing the package's files, which discovery reads to locate the packaged spec."""
    dist_info = site / f"{package.name}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n")
    rows = [f"{path.relative_to(site).as_posix()},," for path in sorted(package.rglob("*")) if path.is_file()]
    rows.append(f"{dist_info.relative_to(site).as_posix()}/METADATA,,")
    rows.append(f"{dist_info.relative_to(site).as_posix()}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")


def _preinstall_probe_into_prefix(site: Path) -> None:
    """Lay a neutral chain-declaring plugin into the prefix site dir: the import package,
    its packaged ``tai-plugin.yml`` and its one-file chain."""
    package = site / _PROBE_IMPORT_PACKAGE
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tai-plugin.yml").write_text(_PROBE_SPEC)
    (package / "migrations" / "0001_baseline.sql").write_text(
        f"CREATE TABLE {_PROBE_TABLE} (id integer PRIMARY KEY);\n"
    )
    _write_distribution(site, _PROBE_DISTRIBUTION, "1.0.0", package)


def _preinstall_accounts_into_prefix(site: Path) -> None:
    """Copy the environment's accounts-postgres distribution into the prefix site dir, the
    way a ``pip install --prefix`` of the same plugin leaves it."""
    source = Path(str(importlib.resources.files(_ACCOUNTS_IMPORT_PACKAGE)))
    target = site / _ACCOUNTS_IMPORT_PACKAGE
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    _write_distribution(site, _ACCOUNTS_DISTRIBUTION, importlib.metadata.version(_ACCOUNTS_DISTRIBUTION), target)


def test_fresh_database_migrates_without_traceback(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A truly fresh database: no tables, no install rows, no prefix, no manifest. The CLI
    # applies the skeleton-owned chains and every environment-installed plugin chain.
    result = _run_migrate(_migrate_env(harness_settings, scratch_db), tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert "Traceback" not in result.stderr
    assert "Applied skeleton 0001_baseline." in result.stdout
    assert f"Applied {_ACCOUNTS_DISTRIBUTION} 0001_baseline." in result.stdout

    # The history records both skeleton-owned chains (the skeleton baseline and the
    # ``states`` record store) and the installed accounts-postgres chain, whose tables
    # exist although no manifest names the plugin.
    assert {"skeleton", "states", _ACCOUNTS_DISTRIBUTION} <= _history_components(harness_settings, scratch_db)
    tables = _public_tables(harness_settings, scratch_db)
    assert "marketplace_installs" in tables
    assert {"accounts_users", "accounts_sessions", "accounts_invites"} <= tables


@pytest.mark.needs("files")
def test_prefix_preinstalled_plugin_chain_applies_with_skeleton(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A fresh database plus a prefix holding the neutral probe and a second copy of
    # accounts-postgres (no install rows): one ``tai db migrate`` lands the probe's chain
    # from the prefix and accounts-postgres's chain exactly once.
    prefix = tmp_path / "plugins-prefix"
    site = Path(prefix_site_dirs(str(prefix))[0])
    site.mkdir(parents=True)
    _preinstall_probe_into_prefix(site)
    _preinstall_accounts_into_prefix(site)
    env = _migrate_env(harness_settings, scratch_db, prefix=prefix)

    result = _run_migrate(env, tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert "Applied skeleton 0001_baseline." in result.stdout
    assert f"Applied {_PROBE_DISTRIBUTION} 0001_baseline." in result.stdout
    assert result.stdout.count(f"Applied {_ACCOUNTS_DISTRIBUTION} 0001_baseline.") == 1, result.stdout

    history = _history_components(harness_settings, scratch_db)
    assert {"skeleton", "states", _ACCOUNTS_DISTRIBUTION, _PROBE_DISTRIBUTION} <= history
    assert _PROBE_TABLE in _public_tables(harness_settings, scratch_db)

    # The probe ran from the prefix scan, not an install record.
    with _connect(harness_settings, scratch_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM marketplace_installs")
        assert cur.fetchone() == (0,)

    # A second run discovers the same single entry per distribution and is an
    # inert no-op — nothing double-applies.
    rerun = _run_migrate(env, tmp_path)
    assert rerun.returncode == 0, f"exit {rerun.returncode}; stderr:\n{rerun.stderr}\nstdout:\n{rerun.stdout}"
    assert "Schema is up to date" in rerun.stdout
