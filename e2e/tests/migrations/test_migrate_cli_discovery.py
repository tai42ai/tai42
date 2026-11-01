"""``tai db migrate`` chain discovery, driven through the REAL CLI over a
subprocess against a live Postgres — the composed path an operator runs.

Two legs pin the CLI's own discovery where a hand-built entry list cannot:

- *fresh database*: a truly empty database (no tables at all, no manifest)
  migrates cleanly — the skeleton-owned chains and the chain of every plugin
  installed in the environment (accounts-postgres among them) land in one run,
  with no traceback.
- *plugin layer*: a neutral chain-declaring plugin laid into the current build of
  the plugin layer the way ``pip install --prefix`` leaves it, with NO marketplace
  install row, has its chain applied in the same run; a second copy of
  accounts-postgres in the layer (shadowed by the environment copy) still gives
  exactly one entry for that distribution, and a second run is an inert no-op.
- *server environment*: a neutral chain-declaring ``mcp-server`` package laid into
  the current build of its own server environment, the virtual environment that runs
  it, has its chain applied, with no marketplace install row.

These need only the shared Postgres, not a booted stack: each leg owns a scratch
database it drops at teardown. The subprocess env carries only the registry's
``TAI_DATABASE_DEFAULT_*`` coordinates (plus ``TAI_PLUGINS_DIR`` on the layer and
server legs), so the CLI resolves its connection exactly as a deployment does.
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

import pytest
from tai42_skeleton.marketplace.environments.layout import (
    layer_dir,
    layer_site_dirs,
    new_build_id,
    server_dir,
    server_site_dirs,
)
from tai42_skeleton.marketplace.environments.swap import point_current

from tai42_e2e.binaries import tai_bin
from tai42_e2e.db_schema import connect, history_components, public_tables
from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.needs("no-stack", "cli", "store:postgres")

_ACCOUNTS_DISTRIBUTION = "tai42-accounts-postgres"
_ACCOUNTS_IMPORT_PACKAGE = "tai42_accounts_postgres"

_PROBE_DISTRIBUTION = "tai42-layerprobe"
_PROBE_IMPORT_PACKAGE = "tai42_layerprobe"
_PROBE_TABLE = "layerprobe_items"
_PROBE_SPEC = f"""\
spec_version: 1
namespace: tai42
name: layerprobe
package: {_PROBE_DISTRIBUTION}
version: 1.0.0
description: A chain-declaring test plugin
license: Apache-2.0
contract: '>=0.1,<1000'
categories: [dev]
provides:
  - kind: tool
    name: layerprobe_ping
    module: {_PROBE_IMPORT_PACKAGE}.tools
    description: Answer a fixed reply
migrations: migrations
"""

_SERVER_PROBE_DISTRIBUTION = "tai42-serverprobe"
_SERVER_PROBE_IMPORT_PACKAGE = "tai42_serverprobe"
_SERVER_PROBE_TABLE = "serverprobe_items"
_SERVER_PROBE_SPEC = f"""\
spec_version: 1
namespace: tai42
name: serverprobe
package: {_SERVER_PROBE_DISTRIBUTION}
version: 1.0.0
description: A chain-declaring test server package
license: Apache-2.0
categories: [dev]
provides:
  - kind: mcp-server
    name: serverprobe_mcp
    description: Launch the test server
    mcp:
      command: tai42-serverprobe-mcp
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


def _migrate_env(harness: HarnessSettings, dbname: str, *, plugins_dir: Path | None = None) -> dict[str, str]:
    """The clean child env an operator's ``tai db migrate`` runs under: the
    registry's default-database coordinates pointing at the scratch database, and
    the plugins directory when the leg opts into one."""
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
    if plugins_dir is not None:
        env["TAI_PLUGINS_DIR"] = str(plugins_dir)
    return env


def _run_migrate(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the REAL ``tai db migrate`` console script — the operator's door."""
    return subprocess.run(
        [tai_bin(), "db", "migrate"], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=180
    )


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


def _current_layer_site(plugins_dir: Path) -> Path:
    """Make a new, empty layer build the current one and return its site directory."""
    layer = layer_dir(plugins_dir)
    build_id = new_build_id()
    site = layer_site_dirs(layer.build(build_id))[0]
    site.mkdir(parents=True)
    point_current(layer, build_id)
    return site


def _current_server_site(plugins_dir: Path, package: str) -> Path:
    """Make a new, empty build of ``package``'s server environment the current one and return its site directory.

    The build records the running Python in its ``pyvenv.cfg``, as ``venv`` writes it, so its site
    directory is the one that interpreter installs into.
    """
    environment = server_dir(plugins_dir, package)
    build_id = new_build_id()
    build = environment.build(build_id)
    build.mkdir(parents=True)
    (build / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\ninclude-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    site = server_site_dirs(build)[0]
    site.mkdir(parents=True)
    point_current(environment, build_id)
    return site


def _lay_probe(site: Path, *, dist_name: str, import_package: str, spec: str, table: str) -> None:
    """Lay a neutral chain-declaring plugin into a site dir: the import package, its
    packaged ``tai-plugin.yml`` and its one-file chain creating ``table``."""
    package = site / import_package
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tai-plugin.yml").write_text(spec)
    (package / "migrations" / "0001_baseline.sql").write_text(f"CREATE TABLE {table} (id integer PRIMARY KEY);\n")
    _write_distribution(site, dist_name, "1.0.0", package)


def _lay_accounts_into_layer(site: Path) -> None:
    """Copy the environment's accounts-postgres distribution into a layer site dir, the
    way a ``pip install --prefix`` of the same plugin leaves it."""
    source = Path(str(importlib.resources.files(_ACCOUNTS_IMPORT_PACKAGE)))
    target = site / _ACCOUNTS_IMPORT_PACKAGE
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    _write_distribution(site, _ACCOUNTS_DISTRIBUTION, importlib.metadata.version(_ACCOUNTS_DISTRIBUTION), target)


def test_fresh_database_migrates_without_traceback(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A truly fresh database: no tables, no install rows, no plugins directory, no manifest. The CLI
    # applies the skeleton-owned chains and every environment-installed plugin chain.
    result = _run_migrate(_migrate_env(harness_settings, scratch_db), tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert "Traceback" not in result.stderr
    assert "Applied skeleton 0001_baseline." in result.stdout
    assert f"Applied {_ACCOUNTS_DISTRIBUTION} 0001_baseline." in result.stdout

    # The history records both skeleton-owned chains (the skeleton baseline and the
    # ``states`` record store) and the installed accounts-postgres chain, whose tables
    # exist although no manifest names the plugin.
    assert {"skeleton", "states", _ACCOUNTS_DISTRIBUTION} <= history_components(harness_settings, scratch_db)
    tables = public_tables(harness_settings, scratch_db)
    assert "marketplace_installs" in tables
    assert {"accounts_users", "accounts_sessions", "accounts_invites"} <= tables


@pytest.mark.needs("files")
def test_a_plugin_layer_chain_applies_with_skeleton(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A fresh database plus a current layer build holding the neutral probe and a second
    # copy of accounts-postgres (no install rows): one ``tai db migrate`` lands the probe's
    # chain from the layer and accounts-postgres's chain exactly once.
    plugins_dir = tmp_path / "plugins"
    site = _current_layer_site(plugins_dir)
    _lay_probe(
        site,
        dist_name=_PROBE_DISTRIBUTION,
        import_package=_PROBE_IMPORT_PACKAGE,
        spec=_PROBE_SPEC,
        table=_PROBE_TABLE,
    )
    _lay_accounts_into_layer(site)
    env = _migrate_env(harness_settings, scratch_db, plugins_dir=plugins_dir)

    result = _run_migrate(env, tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert "Applied skeleton 0001_baseline." in result.stdout
    assert f"Applied {_PROBE_DISTRIBUTION} 0001_baseline." in result.stdout
    assert result.stdout.count(f"Applied {_ACCOUNTS_DISTRIBUTION} 0001_baseline.") == 1, result.stdout

    history = history_components(harness_settings, scratch_db)
    assert {"skeleton", "states", _ACCOUNTS_DISTRIBUTION, _PROBE_DISTRIBUTION} <= history
    assert _PROBE_TABLE in public_tables(harness_settings, scratch_db)

    # The probe ran from the layer scan, not an install record.
    with connect(harness_settings, scratch_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM marketplace_installs")
        assert cur.fetchone() == (0,)

    # A second run discovers the same single entry per distribution and is an
    # inert no-op — nothing double-applies.
    rerun = _run_migrate(env, tmp_path)
    assert rerun.returncode == 0, f"exit {rerun.returncode}; stderr:\n{rerun.stderr}\nstdout:\n{rerun.stdout}"
    assert "Schema is up to date" in rerun.stdout


@pytest.mark.needs("files")
def test_a_server_package_chain_applies_with_skeleton(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path
) -> None:
    # A fresh database plus the current build of a server package's own environment holding
    # the package (no install rows): ``tai db migrate`` reads the package from the
    # environment that runs it and lands its chain.
    plugins_dir = tmp_path / "plugins"
    _lay_probe(
        _current_server_site(plugins_dir, _SERVER_PROBE_DISTRIBUTION),
        dist_name=_SERVER_PROBE_DISTRIBUTION,
        import_package=_SERVER_PROBE_IMPORT_PACKAGE,
        spec=_SERVER_PROBE_SPEC,
        table=_SERVER_PROBE_TABLE,
    )

    result = _run_migrate(_migrate_env(harness_settings, scratch_db, plugins_dir=plugins_dir), tmp_path)

    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    assert f"Applied {_SERVER_PROBE_DISTRIBUTION} 0001_baseline." in result.stdout
    assert _SERVER_PROBE_DISTRIBUTION in history_components(harness_settings, scratch_db)
    assert _SERVER_PROBE_TABLE in public_tables(harness_settings, scratch_db)
