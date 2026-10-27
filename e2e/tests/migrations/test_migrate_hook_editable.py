"""``tai db migrate`` and the boot-time schema gate over a plugin installed with ``pip install -e``
in an import-hook editable mode, driven through the real CLI against a live Postgres.

A scratch hatchling project with ``dev-mode-exact = true`` is installed editable into this
environment. Its ``.dist-info`` records none of its packages: the ``.pth`` file pip writes
holds an ``import`` line, and the finder it installs at interpreter start serves the package
from the project's source tree. So:

- without a manifest, ``tai db migrate`` cannot enumerate the distribution's packages; it
  succeeds, names the distribution and creates no table of it;
- the boot-time schema gate, given a manifest loading a module of the plugin, finds the
  package where the import system serves it and refuses the pending chain;
- ``tai db migrate`` with that manifest persisted applies the chain, and the gate passes.

``pip install`` changes the ONE shared environment, so the install is undone in a
``finally`` — every distribution the install added (pip's installation report names them)
is uninstalled — and the plugin is asserted absent before and after. The build backend is
fetched from the package index into pip's isolated build environment.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import secrets
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from tai42_e2e.binaries import tai_bin
from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.needs("no-stack", "cli", "store:postgres")

_DIST = "tai42-hookprobe"
_TOP_LEVEL = "tai42_hookprobe"
_TOOLS_MODULE = f"{_TOP_LEVEL}.tools"
_TABLE = "hookprobe_items"
_SPEC = f"""\
spec_version: 1
namespace: tai42
name: hookprobe
package: {_DIST}
version: 1.0.0
description: A chain-declaring test plugin
license: Apache-2.0
contract: '>=0.1,<1000'
categories: [dev]
provides:
  - kind: tool
    name: hookprobe_ping
    module: {_TOOLS_MODULE}
    description: Answer a fixed reply
migrations: migrations
"""
_PYPROJECT = f"""\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{_DIST}"
version = "1.0.0"

[tool.hatch.build.targets.wheel]
packages = ["src/{_TOP_LEVEL}"]
dev-mode-exact = true
"""

# Runs the boot-time schema gate the way boot does, over the manifest ``TAI_MANIFEST_PATH``
# names: exit 0 when every loaded chain is applied, the gate's message on stderr otherwise.
_GATE_SCRIPT = textwrap.dedent(
    """\
    import asyncio
    import os
    import sys

    import yaml

    from tai42_skeleton.db.boot_gate import SchemaOutOfDateError, assert_deployment_schemas_applied
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.marketplace.compat import distribution_map

    with open(os.environ["TAI_MANIFEST_PATH"]) as fh:
        manifest = Manifest.model_validate(yaml.safe_load(fh))
    try:
        asyncio.run(assert_deployment_schemas_applied(manifest, distribution_map()))
    except SchemaOutOfDateError as exc:
        print(exc, file=sys.stderr)
        sys.exit(3)
    """
)


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
    """A fresh, EMPTY database dropped at teardown."""
    name = f"tai42_e2e_hookedit_{secrets.token_hex(4)}"
    pg_admin.create_empty_db(name)
    try:
        yield name
    finally:
        pg_admin.drop_stack_db(name)


def _assert_plugin_absent() -> None:
    try:
        importlib.metadata.distribution(_DIST)
    except importlib.metadata.PackageNotFoundError:
        return
    raise RuntimeError(f"{_DIST} is installed in the environment (left by an aborted run?); remove it first")


def _lay_out_project(root: Path) -> Path:
    """The scratch project: its pyproject, and a package shipping the spec, a tools module and a chain."""
    package = root / "src" / _TOP_LEVEL
    (package / "migrations").mkdir(parents=True)
    (root / "pyproject.toml").write_text(_PYPROJECT)
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text("")
    (package / "tai-plugin.yml").write_text(_SPEC)
    (package / "migrations" / "0001_baseline.sql").write_text(f"CREATE TABLE {_TABLE} (id integer PRIMARY KEY);\n")
    return root


def _pip(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args], capture_output=True, text=True, timeout=600, check=False
    )


@pytest.fixture
def hook_editable_plugin(tmp_path: Path) -> Iterator[Path]:
    """The scratch project installed with ``pip install -e``, uninstalled with everything it added."""
    _assert_plugin_absent()
    project = _lay_out_project(tmp_path / "project")
    report = tmp_path / "pip-report.json"
    installed = _pip("install", "--report", str(report), "-e", str(project))
    added: list[str] = []
    try:
        assert installed.returncode == 0, f"pip install -e failed:\n{installed.stdout}\n{installed.stderr}"
        added = [item["metadata"]["name"] for item in json.loads(report.read_text())["install"]]
        assert _DIST in added, added
        yield project
    finally:
        if added:
            removed = _pip("uninstall", "--yes", *added)
            assert removed.returncode == 0, f"pip uninstall failed:\n{removed.stdout}\n{removed.stderr}"
        _assert_plugin_absent()


def _env(harness: HarnessSettings, dbname: str, config_dir: Path) -> dict[str, str]:
    """The clean child env: the default database's coordinates and a config dir holding no manifest yet."""
    venv_bin = str(Path(sys.executable).parent)
    return {
        "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TAI_CONFIG_DIR_PATH": str(config_dir),
        "TAI_MANIFEST_PATH": str(config_dir / "manifest.yml"),
        "TAI_DATABASE_DEFAULT_PG_HOST": harness.pg_host,
        "TAI_DATABASE_DEFAULT_PG_PORT": str(harness.pg_port),
        "TAI_DATABASE_DEFAULT_PG_DB": dbname,
        "TAI_DATABASE_DEFAULT_PG_USER": harness.pg_user,
        "TAI_DATABASE_DEFAULT_PG_PASSWORD": harness.pg_password,
    }


def _run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=180, check=False)


def _report(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"


def _table_exists(harness: HarnessSettings, dbname: str) -> bool:
    with (
        psycopg.connect(
            host=harness.pg_host,
            port=harness.pg_port,
            user=harness.pg_user,
            password=harness.pg_password,
            dbname=dbname,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{_TABLE}",))
        row = cur.fetchone()
        return bool(row and row[0])


def test_hook_served_editable_plugin_is_named_then_migrated_through_the_manifest(
    harness_settings: HarnessSettings, scratch_db: str, tmp_path: Path, hook_editable_plugin: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env = _env(harness_settings, scratch_db, config_dir)
    migrate = [tai_bin(), "db", "migrate"]

    # No manifest: the distribution's packages cannot be enumerated, so it is named and
    # its chain is not applied; everything else migrates and the exit is clean.
    first = _run(migrate, env, tmp_path)
    assert first.returncode == 0, _report(first)
    assert "Applied skeleton 0001_baseline." in first.stdout
    assert f"distribution {_DIST!r} is an editable install served by an import hook" in first.stdout
    assert not _table_exists(harness_settings, scratch_db)

    # A manifest loading the plugin's tools module: the boot gate finds the chain where the
    # import system serves the package, and refuses it while it is pending.
    (config_dir / "manifest.yml").write_text(
        f"default_routers: none\ntools:\n  - title: H\n    module: {_TOOLS_MODULE}\n"
    )
    gate = [sys.executable, "-c", _GATE_SCRIPT]
    refused = _run(gate, env, tmp_path)
    assert refused.returncode == 3, _report(refused)
    assert f"{_DIST}: 1 pending (0001_baseline.sql)" in refused.stderr

    # migrate with that manifest applies the chain, no longer names the distribution, and
    # the gate passes.
    second = _run(migrate, env, tmp_path)
    assert second.returncode == 0, _report(second)
    assert f"Applied {_DIST} 0001_baseline." in second.stdout
    assert f"distribution {_DIST!r}" not in second.stdout
    assert _table_exists(harness_settings, scratch_db)
    passed = _run(gate, env, tmp_path)
    assert passed.returncode == 0, _report(passed)
