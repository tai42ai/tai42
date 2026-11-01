"""Installing an ``mcp-server``-kind plugin item, end to end. Opt-in: collects
only with ``TAI_E2E_MARKETPLACE=1``.

The eta fixture provides ONE item of kind ``mcp-server`` (no module, no
tai42-contract): its ``mcp.command`` launches the fixture's own one-tool stdio MCP
server. Installing it exercises the mcp-entry install path — the installer appends a
manifest ``mcp`` entry titled by the item name and reloads, and the skeleton's MCP
loader spawns the child and binds its tool onto THIS server's ``/mcp`` under the
title prefix. The leg asserts that landed contract:

* After install: the persisted manifest carries an ``mcp`` entry titled
  ``e2e_eta_mcp``, the attribution store records the listing, and the schema chain
  the package declares is applied from its server environment (its history row and
  its table).
* Go-live: ``GET /api/mcp-status`` shows the server bound with its projected tool,
  and that tool answers its marker payload over the product's own ``/mcp``.
* Its own environment: the package is built into a virtual environment of its own
  at ``servers/tai-e2e-market-eta/current`` under ``TAI_PLUGINS_DIR``. The console
  script's shebang names that environment's interpreter, and eta's one dependency
  (``tai-e2e-market-etalib``) is in that environment and in neither the plugin layer
  nor the image.
* Uninstall unmounts: the projected tool leaves ``/mcp`` + ``/api/tools``, the
  manifest ``mcp`` entry is gone, and the server environment is deleted; the
  package's table is kept.

A second leg proves the environment outlives a serve restart: the stdio launch finds
the console script the package declares in the environment's ``bin`` directory (never on
``PATH``), so the server goes live at the install's reload and again after the restart.

Only the package's own console scripts are exposed to the stdio launch: a hand-written
manifest ``mcp`` entry whose command is a bare ``python`` runs the serving image's
interpreter beside the installed eta environment, never the environment's own. A packaged
mcp-server item whose command is not a console script of its package is refused at
install with the builder's text, and nothing is installed.

A bare mcp command names one package's script: installing the xi fixture, whose package
declares the console script eta's package declares, beside an installed eta is refused
with HTTP 409 naming both packages. Eta stays live and is the only installed plugin, and
an unrelated stdio entry still launches.

This leg requires the marketplace registry's mcp-server ingest branch to be present,
so the contract-less fixture seeds and its marketplace pin resolves at registry
install. It is part of the ``TAI_E2E_MARKETPLACE``-gated suite (collected only with
``TAI_E2E_MARKETPLACE=1``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tai42_e2e.catalog_seed import forge_eta_wheel, forge_xi_wheel, publish_eta_wheel, seed_eta_listing, seed_xi_listing
from tai42_e2e.db_schema import history_components, public_tables
from tai42_e2e.fixture_catalog import (
    ETA_MCP_TITLE,
    ETA_MCP_TOOL,
    ETA_PACKAGE,
    ETA_REF,
    ETA_SCRIPT,
    ETALIB_PACKAGE,
    XI_PACKAGE,
    XI_REF,
    XI_VERSION,
    FixtureArtifacts,
)
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._market_support import (
    api_tool_names,
    assert_mcp_server_not_failed,
    cli_env,
    image_holds,
    installed_refs,
    layer_holds,
    manifest_mcp_titles,
    ok_json,
    persisted_manifest,
    plugin_absent,
    run_cli,
    server_environment,
    server_environment_holds,
    uninstall_and_assert_clean,
    wait_eta_mcp_server_live,
)

# The hand-written manifest ``mcp`` entry launched with a bare ``python``: the stdio server
# module the image ships, so the entry needs nothing a plugin environment holds.
_IMAGE_PYTHON_TITLE = "e2e_image_python"
_IMAGE_PYTHON_MODULE = "tai42_e2e_fixtures.misbehaving_mcp_server"
# The entry's liveness tool, bound under the entry's title prefix.
_IMAGE_PYTHON_TOOL = f"{_IMAGE_PYTHON_TITLE}_ok"

# The table eta's one-file schema chain creates.
_ETA_TABLE = "tai_e2e_market_eta_marks"

# The eta version forged with ``command: python``, a command the package does not declare.
_UNDECLARED_COMMAND_VERSION = "0.3.0"


def _assert_eta_in_its_own_environment(stack: TaiStack) -> None:
    """Eta, its console script and its dependency are in eta's own server environment,
    and in neither the plugin layer nor the image."""
    environment = server_environment(stack, ETA_PACKAGE)
    assert environment.is_dir(), f"no server environment at {environment}"
    assert (environment / "pyvenv.cfg").is_file(), f"{environment} is not a virtual environment"
    build = environment.resolve()
    script = environment / "bin" / ETA_SCRIPT
    shebang = script.read_text(encoding="utf-8").splitlines()[0]
    interpreter = shebang.removeprefix("#!").strip()
    assert Path(interpreter).parent == build / "bin", f"{script} runs {interpreter!r}, outside {build}"
    for distribution in (ETA_PACKAGE, ETALIB_PACKAGE):
        assert server_environment_holds(stack, ETA_PACKAGE, distribution), f"{distribution} not in {environment}"
        assert not layer_holds(stack, distribution), f"{distribution} is in the plugin layer"
        assert not image_holds(distribution), f"{distribution} is in the image"


pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
    ),
]


@pytest.mark.needs("mutable", "setting:PIP_INDEX_URL", "setting:TAI_PLUGINS_DIR", "setting:fixture-plugin:eta", "files")
async def test_install_go_live_and_uninstall_mcp_server(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
) -> None:
    stack = marketplace_stack

    # Publish eta into THIS spec's registry only — the mcp-server fixture is kept out
    # of the shared browse catalog, so the leg seeds the fixture it installs itself.
    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)

    # Precondition (negative first): no mcp entry, the projected tool is dark
    # everywhere, and no plugin environment holds the distribution.
    assert ETA_MCP_TITLE not in manifest_mcp_titles(stack)
    assert ETA_MCP_TOOL not in await api_tool_names(stack)
    async with stack.mcp() as mcp:
        assert ETA_MCP_TOOL not in await mcp.tool_names()
    assert plugin_absent(stack, ETA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ETA_REF, "version": "0.1.0"})

    # The installer appended the manifest ``mcp`` entry titled by the item name, and
    # the attribution store recorded the listing.
    assert ETA_MCP_TITLE in manifest_mcp_titles(stack), (
        f"install did not append the manifest mcp entry {ETA_MCP_TITLE!r}"
    )
    installed = await installed_refs(stack)
    assert ETA_REF in installed, f"attribution row missing for {ETA_REF!r}: {sorted(installed)}"
    assert installed[ETA_REF]["version"] == "0.1.0"

    # The package was built into its own virtual environment, which also resolved its
    # dependency; neither the plugin layer nor the image holds either distribution.
    _assert_eta_in_its_own_environment(stack)

    # The install applied the chain the package declares, read from that environment.
    harness, dbname = stack.infra.settings, stack.resources.pg_db
    assert ETA_PACKAGE in history_components(harness, dbname), "eta's schema chain was not applied"
    assert _ETA_TABLE in public_tables(harness, dbname)

    # Go-live: the reload mounted the child, bound its tool onto ``/mcp`` under the
    # title, and the tool answers its marker payload over the product's own MCP.
    await wait_eta_mcp_server_live(stack)
    assert ETA_MCP_TOOL in await api_tool_names(stack), "the mounted tool is not on /api/tools after go-live"
    await assert_mcp_server_not_failed(stack, ETA_MCP_TITLE)

    # Uninstall unmounts: the tool leaves /mcp + /api/tools, the manifest mcp entry is
    # gone, and the server environment is deleted.
    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )
    assert not server_environment(stack, ETA_PACKAGE).parent.exists()
    # The schema follows what is installed forward only: the uninstall keeps the table.
    assert _ETA_TABLE in public_tables(harness, dbname)


@pytest.mark.needs(
    "mutable", "process", "setting:PIP_INDEX_URL", "setting:TAI_PLUGINS_DIR", "setting:fixture-plugin:eta", "files"
)
async def test_an_mcp_server_environment_survives_a_serve_restart(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
) -> None:
    stack = marketplace_stack

    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)
    assert plugin_absent(stack, ETA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ETA_REF, "version": "0.1.0"})

    # Go-live at the install's reload: the console script launched from the server
    # environment imports its package and its tool answers over the product's ``/mcp``.
    await wait_eta_mcp_server_live(stack)

    # A real serve-process restart: the cold boot launches the environment's script again.
    stack.restart("serve")
    await wait_eta_mcp_server_live(stack)
    await assert_mcp_server_not_failed(stack, ETA_MCP_TITLE)

    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )


async def test_eta_preview_reports_no_required_env_and_package_delivery(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
) -> None:
    stack = marketplace_stack

    # Publish eta into THIS spec's registry only (kept out of the shared browse catalog).
    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)

    # eta's mcp block carries NO ``!ENV`` markers, so the preview's required_env is empty;
    # and eta ships a package (its mcp.command is a console script), so delivery is
    # ``package`` — the server-environment path the go-live spec above exercises.
    preview = await stack.api().post("/api/marketplace/install/preview", json={"ref": ETA_REF, "version": "0.1.0"})
    assert preview["required_env"] == [], preview
    assert preview["missing_env"] == [], preview
    assert preview["delivery"] == "package", preview


async def _replace_manifest(stack: TaiStack, document: dict[str, Any]) -> None:
    resp = await stack.api().request_raw(
        "POST", "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)}, timeout=60.0
    )
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"


async def _wait_title_bound(stack: TaiStack, title: str) -> None:
    async def bound() -> bool:
        status = await stack.api().get("/api/mcp-status")
        return bool((status.get("bound") or {}).get(title))

    await wait_for_async(bound, deadline=45.0, message=f"mcp server {title!r} was never bound")


@pytest.mark.needs(
    "mutable", "process", "setting:PIP_INDEX_URL", "setting:TAI_PLUGINS_DIR", "setting:fixture-plugin:eta", "files"
)
async def test_a_bare_python_entry_runs_the_image_interpreter_beside_an_installed_server(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
) -> None:
    stack = marketplace_stack

    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)
    await stack.api().post("/api/marketplace/install", json={"ref": ETA_REF, "version": "0.1.0"})
    await wait_eta_mcp_server_live(stack)
    assert (server_environment(stack, ETA_PACKAGE) / "bin" / "python").exists()

    document = persisted_manifest(stack)
    entry = {
        "title": _IMAGE_PYTHON_TITLE,
        "config": {"type": "stdio", "command": "python", "args": ["-m", _IMAGE_PYTHON_MODULE]},
    }
    await _replace_manifest(stack, {**document, "mcp": [*(document.get("mcp") or []), entry]})
    await _wait_title_bound(stack, _IMAGE_PYTHON_TITLE)

    # The bare ``python`` resolved on the serve process's PATH, to the image's interpreter:
    # the entry's server module and its ``mcp`` dependency import only there, never in the
    # eta environment, whose own ``bin/python`` is not exposed to the stdio launch.
    data = await stack.api().post("/api/run-tool", json={"tool_name": _IMAGE_PYTHON_TOOL, "arguments": {}})
    assert data == {"kind": "result", "result": {"ok": True}}, data
    await wait_eta_mcp_server_live(stack)

    current = persisted_manifest(stack)
    await _replace_manifest(
        stack, {**current, "mcp": [m for m in current.get("mcp") or [] if m["title"] != _IMAGE_PYTHON_TITLE]}
    )
    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )


@pytest.mark.needs("mutable", "setting:PIP_INDEX_URL", "setting:TAI_PLUGINS_DIR", "setting:fixture-plugin:eta", "files")
async def test_an_mcp_server_command_its_package_does_not_declare_is_refused_at_install(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
    tmp_path: Path,
) -> None:
    stack = marketplace_stack

    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)
    wheel = forge_eta_wheel(_UNDECLARED_COMMAND_VERSION, tmp_path, mcp_command="python")
    await publish_eta_wheel(marketplace_service, package_index, wheel)

    body = await stack.api().post(
        "/api/marketplace/install", json={"ref": ETA_REF, "version": _UNDECLARED_COMMAND_VERSION}, expect=409
    )

    assert body["error"] == (
        f"{ETA_REF}: mcp-server item {ETA_MCP_TITLE} names command 'python', which is not a console script of "
        f"package {ETA_PACKAGE} (its scripts: {ETA_SCRIPT}); an mcp-server command is a console script the "
        "package declares"
    ), body
    # Nothing was installed: no row, no manifest entry, no server environment.
    assert ETA_REF not in await installed_refs(stack)
    assert ETA_MCP_TITLE not in manifest_mcp_titles(stack)
    assert plugin_absent(stack, ETA_PACKAGE)


@pytest.mark.needs(
    "mutable",
    "process",
    "cli",
    "setting:PIP_INDEX_URL",
    "setting:TAI_PLUGINS_DIR",
    "setting:fixture-plugin:eta",
    "setting:fixture-plugin:xi",
    "files",
)
async def test_a_server_package_declaring_a_script_another_server_exposes_is_refused_at_install(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
    tmp_path: Path,
) -> None:
    stack = marketplace_stack

    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)
    await seed_xi_listing(marketplace_service, package_index, forge_xi_wheel(tmp_path))
    await stack.api().post("/api/marketplace/install", json={"ref": ETA_REF, "version": "0.1.0"})
    await wait_eta_mcp_server_live(stack)

    body = await stack.api().post("/api/marketplace/install", json={"ref": XI_REF, "version": XI_VERSION}, expect=409)

    assert body["error"] == (
        f"cannot install {XI_REF} {XI_VERSION}: its package {XI_PACKAGE} declares the console script "
        f"{ETA_SCRIPT!r}, which the installed server package {ETA_PACKAGE} already exposes; a bare mcp command "
        f"names one package's script — uninstall the plugin that provides {ETA_PACKAGE} first, or choose a "
        f"release that does not declare {ETA_SCRIPT!r}"
    ), body
    # Xi left nothing behind, and eta stays live and is the only installed plugin.
    assert plugin_absent(stack, XI_PACKAGE)
    await wait_eta_mcp_server_live(stack)
    await assert_mcp_server_not_failed(stack, ETA_MCP_TITLE)
    home = tmp_path / "cli-home"
    home.mkdir()
    installed = ok_json(run_cli(cli_env(stack, home), "installed"))["installed"]
    assert [row["ref"] for row in installed] == [ETA_REF], installed

    # The stdio launch still resolves every command: an unrelated entry binds and answers.
    document = persisted_manifest(stack)
    entry = {
        "title": _IMAGE_PYTHON_TITLE,
        "config": {"type": "stdio", "command": "python", "args": ["-m", _IMAGE_PYTHON_MODULE]},
    }
    await _replace_manifest(stack, {**document, "mcp": [*(document.get("mcp") or []), entry]})
    await _wait_title_bound(stack, _IMAGE_PYTHON_TITLE)
    data = await stack.api().post("/api/run-tool", json={"tool_name": _IMAGE_PYTHON_TOOL, "arguments": {}})
    assert data == {"kind": "result", "result": {"ok": True}}, data

    current = persisted_manifest(stack)
    await _replace_manifest(
        stack, {**current, "mcp": [m for m in current.get("mcp") or [] if m["title"] != _IMAGE_PYTHON_TITLE]}
    )
    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )
