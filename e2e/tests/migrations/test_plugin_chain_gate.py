"""The startup schema check over a plugin chain, driven through a live stack.

A chain-declaring plugin distribution sits on the serving processes' ``sys.path``
(installed, not loaded by the manifest), with its chain's database binding declared
and the chain never applied. Replacing the manifest with one that loads the plugin's
tools module is refused by the reload's schema check, naming the pending chain and
``tai db migrate``, while the running epoch keeps serving. ``tai db migrate`` then
discovers the installed chain and applies it, and the next
reload serves the plugin's tool.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
import yaml

from tai42_e2e import Infra, StackConfig, StackResources
from tai42_e2e.binaries import tai_bin
from tai42_e2e.booting import boot_stack
from tai42_e2e.child_env import child_env
from tai42_e2e.manifests import build_bare_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import Variants

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("mutable", "process", "files", "cli", "store:postgres", "setting:PYTHONPATH"),
]

_DIST = "tai42-gateprobe"
_TOP_LEVEL = "tai42_gateprobe"
_TOOLS_MODULE = f"{_TOP_LEVEL}.tools"
_COMPONENT = "gateprobe-store"
_CHAIN_FILE = "0001_items.sql"
_TABLE = "gateprobe_items"

_SPEC = f"""\
spec_version: 1
namespace: tai42
name: gateprobe
package: {_DIST}
version: 1.0.0
description: A chain-declaring test plugin
license: Apache-2.0
contract: '>=0.1,<1000'
categories: [dev]
provides:
  - kind: tool
    name: gateprobe_ping
    module: {_TOOLS_MODULE}
    description: Answer a fixed reply
migrations: migrations
migrations_component: {_COMPONENT}
"""

_TOOLS = '''\
from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def gateprobe_ping() -> str:
    """Answer a fixed reply."""
    return "gateprobe-pong"
'''


def _lay_out_distribution(site: Path) -> None:
    """Write the plugin into ``site`` the way ``pip install`` leaves it: the import package,
    its packaged ``tai-plugin.yml`` and chain, and a ``.dist-info`` whose ``RECORD`` lists them."""
    package = site / _TOP_LEVEL
    (package / "migrations").mkdir(parents=True)
    files = {
        package / "__init__.py": "",
        package / "tools.py": _TOOLS,
        package / "tai-plugin.yml": _SPEC,
        package / "migrations" / _CHAIN_FILE: f"CREATE TABLE {_TABLE} (id integer PRIMARY KEY);\n",
    }
    for path, body in files.items():
        path.write_text(body, encoding="utf-8")
    dist_info = site / f"{_TOP_LEVEL}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {_DIST}\nVersion: 1.0.0\n", encoding="utf-8")
    rows = [f"{path.relative_to(site).as_posix()},," for path in files]
    rows += [f"{dist_info.name}/METADATA,,", f"{dist_info.name}/RECORD,,"]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture
def gateprobe_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    """The bare stack with the plugin installed on every process's ``sys.path`` and its
    chain's binding declared on the stack's own database; the manifest does not load it."""
    site = tmp_path_factory.mktemp("gateprobe-site")
    _lay_out_distribution(site)

    def builder(res: StackResources, variants: Variants) -> StackConfig:
        base = build_bare_stack(res, variants)
        env = {**base.env, "PYTHONPATH": str(site), "TAI_DB_BINDING_GATEPROBE_STORE": "default"}
        return replace(base, name="plugin-chain-gate", env=env)

    yield from boot_stack(infra, tmp_path_factory.mktemp("plugin-chain-gate"), builder)


def _manifest_path(stack: TaiStack) -> Path:
    return stack.root / "config" / "manifest.yml"


def _table_exists(stack: TaiStack) -> bool:
    res = stack.resources
    dsn = f"host={res.pg_host} port={res.pg_port} dbname={res.pg_db} user={res.pg_user} password={res.pg_password}"
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT to_regclass(%s)", (_TABLE,)).fetchone()
    return row is not None and row[0] is not None


async def _tool_names(stack: TaiStack) -> set[str]:
    async with stack.mcp() as mcp:
        return {tool.name for tool in await mcp.list_tools()}


async def test_pending_plugin_chain_refuses_the_reload_until_migrated(
    gateprobe_stack: TaiStack, tmp_path: Path
) -> None:
    stack = gateprobe_stack
    api = stack.api()
    assert "gateprobe_ping" not in await _tool_names(stack)

    document = yaml.safe_load(_manifest_path(stack).read_text(encoding="utf-8")) or {}
    document["tools"] = [*document.get("tools", []), {"title": _TOOLS_MODULE, "module": _TOOLS_MODULE}]
    resp = await api.request_raw(
        "POST", "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)}, timeout=60.0
    )

    # The reload's schema check refuses the build: the pending plugin chain and the fix
    # are named, the plugin's table was never created, and the running epoch still serves
    # without the plugin's tool.
    assert f"{_COMPONENT}: 1 pending ({_CHAIN_FILE})" in resp.text, f"{resp.status_code} {resp.text}"
    assert "tai db migrate" in resp.text, resp.text
    assert not _table_exists(stack)
    names = await _tool_names(stack)
    assert "gateprobe_ping" not in names
    assert names, "the running epoch stopped serving its tools"

    # ``tai db migrate`` under the deployment's env discovers the installed chain
    # and applies it on the bound database.
    migrate = subprocess.run(
        [tai_bin(), "db", "migrate"],
        env=child_env(stack, str(tmp_path), "db-migrate"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert migrate.returncode == 0, f"tai db migrate failed:\n{migrate.stdout}\n{migrate.stderr}"
    assert _COMPONENT in migrate.stdout, migrate.stdout
    assert _table_exists(stack)

    # The next reload passes the schema check and the plugin's tool answers.
    report = await api.post("/api/config/reload", json={}, retry_on_reloading=True, timeout=60.0)
    outcomes = {r["name"]: r["outcome"] for r in report["results"]}
    assert outcomes, report
    assert all(outcome == "applied" for outcome in outcomes.values()), report
    async with stack.mcp() as mcp:
        result = await mcp.call_tool("gateprobe_ping", {})
    assert result.data == "gateprobe-pong", result
