"""A server process verifies its plugin environments against the install store when it starts.
Opt-in: collects only with ``TAI_E2E_MARKETPLACE=1``.

An installed plugin whose plugin layer has lost its ``current`` build no longer matches the
install store: a fresh serve process refuses to start, naming the out-of-date layer and
``tai plugins rebuild``. The real ``tai plugins rebuild``, run with the deployment's env,
builds the layer again from the store, and the same serve process then starts and serves
the plugin's tool from the rebuilt layer.

Server environments another Python built are a finding the rebuild repairs: with two
mcp-server packages installed (eta and omicron), each server environment is made to read
as one an image with an older Python minor version built. A fresh serve process refuses to
start, naming each environment, the two Python versions and ``tai plugins rebuild``; the
real rebuild builds both again for this image, and the same serve process then starts with
both servers live.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tai42_e2e.catalog_seed import forge_omicron_wheel, seed_eta_listing, seed_omicron_listing
from tai42_e2e.fixture_catalog import (
    ALPHA_PACKAGE,
    ALPHA_REF,
    ETA_MCP_TITLE,
    ETA_MCP_TOOL,
    ETA_PACKAGE,
    ETA_REF,
    OMICRON_MCP_TITLE,
    OMICRON_MCP_TOOL,
    OMICRON_PACKAGE,
    OMICRON_REF,
    OMICRON_VERSION,
    FixtureArtifacts,
)
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack

from ._market_support import (
    MarketInstaller,
    layer_holds,
    run_rebuild,
    server_environment,
    stack_plugins_dir,
    uninstall_and_assert_clean,
    wait_eta_mcp_server_live,
    wait_eta_server_mount_live,
    wait_tool_live,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "mutable",
        "files",
        "process",
        "cli",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
        "setting:PIP_INDEX_URL",
        "setting:TAI_PLUGINS_DIR",
    ),
]

_TOOL = "e2e_market_probe"
# The build record every plugin environment build carries.
_BUILD_RECORD = "tai42-build.json"


@pytest.mark.needs("setting:fixture-plugin:alpha")
async def test_a_serve_start_refuses_an_out_of_date_layer_until_it_is_rebuilt(
    marketplace_stack: TaiStack, market_installer: MarketInstaller, tmp_path: Path
) -> None:
    stack = marketplace_stack

    await market_installer.install(stack, ALPHA_REF, ALPHA_PACKAGE, version="0.1.0")
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")
    assert layer_holds(stack, ALPHA_PACKAGE)

    # The layer loses its current build while the install store still records alpha.
    (stack_plugins_dir(stack) / "layer" / "current").unlink()

    # A fresh serve process verifies the plugins directory before it imports a plugin and
    # exits; the readiness wait surfaces its refusal from the log tail.
    with pytest.raises(RuntimeError) as refused:
        stack.restart("serve")
    text = str(refused.value)
    assert "plugin environment is out of date — refusing to start" in text, text
    assert "no current build" in text, text
    assert "Run 'tai plugins rebuild' against this deployment, then start again" in text, text

    rebuilt = run_rebuild(stack, tmp_path)
    assert rebuilt.returncode == 0, f"rebuild failed (exit {rebuilt.returncode}): {rebuilt.stdout}\n{rebuilt.stderr}"
    assert "rebuilt" in rebuilt.stdout, rebuilt.stdout
    assert "running servers load the rebuilt plugin layer on their next start" in rebuilt.stdout, rebuilt.stdout
    assert layer_holds(stack, ALPHA_PACKAGE)

    # The same serve process starts and serves the plugin from the rebuilt layer.
    stack.restart("serve")
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)


def _built_by_an_older_python(build: Path) -> str:
    """Make the server environment ``build`` read as one an image with an older Python minor built.

    Its site directory, its ``pyvenv.cfg`` and its build record name that version, as they
    do once an image upgrade leaves the environment behind. Returns the older version.
    """
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    older = f"{sys.version_info.major}.{sys.version_info.minor - 1}"
    (site_root,) = (build / "lib").iterdir()
    site_root.rename(build / "lib" / site_root.name.replace(running, older))
    config = build / "pyvenv.cfg"
    config.write_text(config.read_text(encoding="utf-8").replace(running, older), encoding="utf-8")
    record = json.loads((build / _BUILD_RECORD).read_text(encoding="utf-8"))
    record["python"] = older
    (build / _BUILD_RECORD).write_text(json.dumps(record), encoding="utf-8")
    return older


@pytest.mark.needs("setting:fixture-plugin:eta", "setting:fixture-plugin:omicron")
async def test_a_serve_start_refuses_server_environments_another_python_built_until_they_are_rebuilt(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
    market_installer: MarketInstaller,
    tmp_path: Path,
) -> None:
    stack = marketplace_stack
    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)
    await seed_omicron_listing(marketplace_service, package_index, forge_omicron_wheel(tmp_path))

    await market_installer.install(stack, ETA_REF, ETA_PACKAGE, version="0.1.0")
    await market_installer.install(stack, OMICRON_REF, OMICRON_PACKAGE, version=OMICRON_VERSION)
    await wait_eta_mcp_server_live(stack)
    await wait_eta_server_mount_live(stack, title=OMICRON_MCP_TITLE, tool=OMICRON_MCP_TOOL)

    # Both server environments now read as ones an image with an older Python built.
    older = ""
    for package in (ETA_PACKAGE, OMICRON_PACKAGE):
        older = _built_by_an_older_python(server_environment(stack, package).resolve())

    # A fresh serve process verifies the plugins directory and refuses to start, naming
    # each server environment's Python and the rebuild.
    with pytest.raises(RuntimeError) as refused:
        stack.restart("serve")
    text = str(refused.value)
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert f"was built for Python {older}" in text, text
    assert f"but this image runs Python {running}" in text, text
    assert "refusing to start" in text, text
    assert "Run 'tai plugins rebuild' against this image" in text, text

    rebuilt = run_rebuild(stack, tmp_path / "home")
    assert rebuilt.returncode == 0, f"rebuild failed (exit {rebuilt.returncode}): {rebuilt.stdout}\n{rebuilt.stderr}"
    for package in (ETA_PACKAGE, OMICRON_PACKAGE):
        (row,) = [line for line in rebuilt.stdout.splitlines() if f"server {package}" in line]
        assert "rebuilt" in row, rebuilt.stdout
        assert (server_environment(stack, package) / "lib" / f"python{running}").is_dir()

    # The same serve process starts, and both servers go live from their rebuilt environments.
    stack.restart("serve")
    await wait_eta_mcp_server_live(stack)
    await wait_eta_server_mount_live(stack, title=OMICRON_MCP_TITLE, tool=OMICRON_MCP_TOOL)

    await uninstall_and_assert_clean(
        stack, OMICRON_REF, package=OMICRON_PACKAGE, tool_name=OMICRON_MCP_TOOL, mcp_title=OMICRON_MCP_TITLE
    )
    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )
