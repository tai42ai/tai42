"""``tai db migrate`` chain discovery, driven through the REAL CLI over a
subprocess against a live Postgres — the composed path an operator runs.

Two legs pin the CLI's own discovery where a hand-built entry list cannot:

- *fresh database*: a truly empty database (no tables at all) migrates cleanly —
  the skeleton chain applies FIRST, and plugin discovery (which reads the
  skeleton-owned marketplace install store) runs only after the baseline exists,
  so the command never tracebacks on the missing table.
- *prefix-preinstalled plugin*: a real platform plugin (accounts-postgres) laid
  into the plugins prefix the way ``pip install --prefix`` leaves it, with NO
  marketplace install row, is discovered by the prefix scan and its declared
  chain applied in the same run — skeleton and plugin chains both land, exactly
  one entry per distribution, and a second run is an inert no-op.

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

from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import tai_bin

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


def _preinstall_accounts_into_prefix(prefix: Path) -> None:
    """Lay the installed accounts-postgres distribution into the prefix the way a
    ``pip install --prefix`` leaves it: the import-package tree (carrying its
    packaged ``tai-plugin.yml`` and ``migrations/`` chain) under the prefix's site
    dir, plus a ``.dist-info`` whose ``RECORD`` lists the package's files — the
    metadata the prefix scan enumerates distributions and locates specs from.
    Deliberately NO marketplace install row: the leg proves discovery without one.
    """
    site = Path(prefix_site_dirs(str(prefix))[0])
    site.mkdir(parents=True, exist_ok=True)
    source = Path(str(importlib.resources.files(_ACCOUNTS_IMPORT_PACKAGE)))
    target = site / _ACCOUNTS_IMPORT_PACKAGE
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    version = importlib.metadata.version(_ACCOUNTS_DISTRIBUTION)
    dist_info = site / f"{_ACCOUNTS_IMPORT_PACKAGE}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {_ACCOUNTS_DISTRIBUTION}\nVersion: {version}\n")
    rows = [f"{path.relative_to(site)},," for path in sorted(target.rglob("*")) if path.is_file()]
    rows.append(f"{dist_info.relative_to(site)}/METADATA,,")
    rows.append(f"{dist_info.relative_to(site)}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")


def test_fresh_database_migrates_without_traceback(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A truly fresh database: no tables, no install rows, no prefix. The CLI must
    # apply the skeleton baseline cleanly — never traceback on the not-yet-created
    # marketplace install store its plugin discovery reads.
    result = _run_migrate(_migrate_env(harness_settings, scratch_db), tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert "Traceback" not in result.stderr
    assert "Applied skeleton 0001_baseline." in result.stdout

    # The baseline really landed: the history records the skeleton chain and the
    # marketplace install store (a skeleton-owned table) now exists.
    assert _history_components(harness_settings, scratch_db) == {"skeleton"}
    assert "marketplace_installs" in _public_tables(harness_settings, scratch_db)


def test_prefix_preinstalled_plugin_chain_applies_with_skeleton(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A fresh database plus a prefix-preinstalled accounts-postgres (no install
    # row): one ``tai db migrate`` lands the skeleton baseline AND the plugin's
    # declared chain, discovered from the prefix scan.
    prefix = tmp_path / "plugins-prefix"
    _preinstall_accounts_into_prefix(prefix)
    env = _migrate_env(harness_settings, scratch_db, prefix=prefix)

    result = _run_migrate(env, tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert "Applied skeleton 0001_baseline." in result.stdout
    assert f"Applied {_ACCOUNTS_DISTRIBUTION} 0001_baseline." in result.stdout

    # Both chains are recorded, and the plugin's own tables exist.
    assert _history_components(harness_settings, scratch_db) == {"skeleton", _ACCOUNTS_DISTRIBUTION}
    tables = _public_tables(harness_settings, scratch_db)
    assert {"accounts_users", "accounts_sessions", "accounts_invites"} <= tables

    # The plugin ran from the prefix scan, not an install record.
    with _connect(harness_settings, scratch_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM marketplace_installs")
        assert cur.fetchone() == (0,)

    # A second run discovers the same single entry per distribution and is an
    # inert no-op — nothing double-applies.
    rerun = _run_migrate(env, tmp_path)
    assert rerun.returncode == 0, f"exit {rerun.returncode}; stderr:\n{rerun.stderr}\nstdout:\n{rerun.stdout}"
    assert "Schema is up to date" in rerun.stdout
