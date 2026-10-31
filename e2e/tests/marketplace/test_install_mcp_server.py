"""Installing an ``mcp-server``-kind plugin item, end to end. Opt-in: collects
only with ``TAI_E2E_MARKETPLACE=1``.

The eta fixture provides ONE item of kind ``mcp-server`` (no module, no
tai42-contract): its ``mcp.command`` launches the fixture's own one-tool stdio MCP
server. Installing it exercises the mcp-entry install path — the installer appends a
manifest ``mcp`` entry titled by the item name and reloads, and the skeleton's MCP
loader spawns the child and binds its tool onto THIS server's ``/mcp`` under the
title prefix. The leg asserts that landed contract:

* After install: the persisted manifest carries an ``mcp`` entry titled
  ``e2e_eta_mcp``, and the attribution store records the listing.
* Go-live: ``GET /api/mcp-status`` shows the server bound with its projected tool,
  and that tool answers its marker payload over the product's own ``/mcp``.
* Uninstall unmounts: the projected tool leaves ``/mcp`` + ``/api/tools``, the
  manifest ``mcp`` entry is gone, and the venv distribution is removed.

A second leg installs the same item into a persistent plugin prefix
(``TAI_PLUGINS_PREFIX``): the console script lands in the prefix's scripts directory,
which is not on ``PATH`` and whose interpreter does not see the prefix on its own. The
stdio launch finds the script there and gives the child the prefix on its
``PYTHONPATH``, so the server goes live at the install's reload and again after a
serve restart; the uninstall removes the script with the distribution.

This leg requires the marketplace registry's mcp-server ingest branch to be present,
so the contract-less fixture seeds and its marketplace pin resolves at registry
install. It is part of the ``TAI_E2E_MARKETPLACE``-gated suite (collected only with
``TAI_E2E_MARKETPLACE=1``).
"""

from __future__ import annotations

import sysconfig
from pathlib import Path

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.catalog_seed import seed_eta_listing
from tai42_e2e.fixture_catalog import ETA_MCP_TITLE, ETA_MCP_TOOL, ETA_PACKAGE, ETA_REF, FixtureArtifacts
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack

from ._market_support import (
    api_tool_names,
    distribution_absent,
    installed_refs,
    manifest_mcp_titles,
    probe_payload,
    uninstall_and_assert_clean,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
    ),
]


async def _mcp_status_bound(stack: TaiStack) -> dict[str, list[str]]:
    """The live MCP-binding snapshot's ``bound`` map (title → tool names)."""
    status = await stack.api().get("/api/mcp-status")
    bound = status.get("bound") if isinstance(status, dict) else None
    if not isinstance(bound, dict):
        raise AssertionError(f"/api/mcp-status returned no 'bound' map: {status!r}")
    return bound


async def _wait_mcp_server_live(stack: TaiStack, *, deadline: float = 45.0) -> None:
    """Wait until the mounted eta server is bound with its projected tool AND that
    tool answers its marker over ``/mcp``.

    A fresh MCP session per poll so a just-reloaded mount is observed. The mount is
    a spawned stdio child probed at reload, so this absorbs the spawn/probe tail."""

    async def live() -> bool:
        if ETA_MCP_TOOL not in _bound_tools(await _mcp_status_bound(stack)):
            return False
        async with stack.mcp() as mcp:
            if ETA_MCP_TOOL not in await mcp.tool_names():
                return False
            result = await mcp.call_tool(ETA_MCP_TOOL, {}, raise_on_error=False)
        if result.is_error:
            return False
        return probe_payload(result).get("eta") == "pong"

    await wait_for_async(
        live, deadline=deadline, message=f"mcp server {ETA_MCP_TITLE!r} never went live with tool {ETA_MCP_TOOL!r}"
    )


def _bound_tools(bound: dict[str, list[str]]) -> set[str]:
    return set(bound.get(ETA_MCP_TITLE) or [])


# The console script the eta distribution installs (its ``[project.scripts]`` entry).
_ETA_SCRIPT = "tai-e2e-market-eta-mcp"
_ETA_DIST = "tai_e2e_market_eta"


def _stack_prefix(stack: TaiStack) -> Path:
    """The stack's persistent plugin prefix (``TAI_PLUGINS_PREFIX``)."""
    return Path(stack.config.env["TAI_PLUGINS_PREFIX"])


def _prefix_eta_dist_present(prefix: Path) -> bool:
    """Whether the eta distribution's ``.dist-info`` is installed under ``prefix``, read from disk."""
    return any(list(site.glob(f"{_ETA_DIST}-*.dist-info")) for site in prefix.glob("lib/python*/site-packages"))


def _prefix_eta_script(prefix: Path) -> Path:
    """Where ``pip install --prefix`` writes the eta console script."""
    return Path(sysconfig.get_paths(vars={"base": str(prefix), "platbase": str(prefix)})["scripts"]) / _ETA_SCRIPT


@pytest.mark.needs("mutable", "setting:PIP_INDEX_URL", "setting:shared-venv", "setting:fixture-plugin:eta", "files")
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
    # everywhere, and the distribution is not in the venv.
    assert ETA_MCP_TITLE not in manifest_mcp_titles(stack)
    assert ETA_MCP_TOOL not in await api_tool_names(stack)
    async with stack.mcp() as mcp:
        assert ETA_MCP_TOOL not in await mcp.tool_names()
    assert distribution_absent(ETA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ETA_REF, "version": "0.1.0"})

    # The installer appended the manifest ``mcp`` entry titled by the item name, and
    # the attribution store recorded the listing.
    assert ETA_MCP_TITLE in manifest_mcp_titles(stack), (
        f"install did not append the manifest mcp entry {ETA_MCP_TITLE!r}"
    )
    installed = await installed_refs(stack)
    assert ETA_REF in installed, f"attribution row missing for {ETA_REF!r}: {sorted(installed)}"
    assert installed[ETA_REF]["version"] == "0.1.0"

    # Go-live: the reload mounted the child, bound its tool onto ``/mcp`` under the
    # title, and the tool answers its marker payload over the product's own MCP.
    await _wait_mcp_server_live(stack)
    assert ETA_MCP_TOOL in await api_tool_names(stack), "the mounted tool is not on /api/tools after go-live"
    status = await stack.api().get("/api/mcp-status")
    failed_titles = {row["title"] for row in (status.get("failed") or [])}
    assert ETA_MCP_TITLE not in failed_titles, f"mcp server {ETA_MCP_TITLE!r} is in the failed list: {status!r}"

    # Uninstall unmounts: the tool leaves /mcp + /api/tools, the manifest mcp entry is
    # gone, and the distribution is removed from the venv.
    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )


@pytest.mark.needs(
    "mutable", "process", "setting:PIP_INDEX_URL", "setting:TAI_PLUGINS_PREFIX", "setting:fixture-plugin:eta", "files"
)
async def test_prefix_install_mcp_server_goes_live_and_survives_a_restart(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_prefix_stack: TaiStack,
) -> None:
    stack = marketplace_prefix_stack
    prefix = _stack_prefix(stack)

    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)

    # Precondition (negative first): no mcp entry, nothing in the prefix, and the
    # distribution is not in the venv.
    assert ETA_MCP_TITLE not in manifest_mcp_titles(stack)
    assert not _prefix_eta_dist_present(prefix)
    assert not _prefix_eta_script(prefix).exists()
    assert distribution_absent(ETA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ETA_REF, "version": "0.1.0"})

    # The install landed IN THE PREFIX: the manifest carries the mcp entry, the
    # distribution and its console script are on disk under the prefix, and the
    # harness venv still cannot resolve the distribution.
    assert ETA_MCP_TITLE in manifest_mcp_titles(stack), (
        f"install did not append the manifest mcp entry {ETA_MCP_TITLE!r}"
    )
    assert _prefix_eta_dist_present(prefix)
    assert _prefix_eta_script(prefix).is_file()
    assert distribution_absent(ETA_PACKAGE)

    # Go-live at the install's reload: the console script launched from the prefix
    # imports its package and its tool answers over the product's own ``/mcp``.
    await _wait_mcp_server_live(stack)

    # A real serve-process restart: the cold boot launches the prefix script again.
    stack.restart("serve")
    await _wait_mcp_server_live(stack)
    status = await stack.api().get("/api/mcp-status")
    failed_titles = {row["title"] for row in (status.get("failed") or [])}
    assert ETA_MCP_TITLE not in failed_titles, f"mcp server {ETA_MCP_TITLE!r} is in the failed list: {status!r}"

    # Uninstall unmounts and removes the plugin's files from the prefix by its RECORD,
    # the console script included.
    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )
    assert not _prefix_eta_dist_present(prefix)
    assert not _prefix_eta_script(prefix).exists()


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
    # ``package`` — the pip-installed path the go-live spec above exercises.
    preview = await stack.api().post("/api/marketplace/install/preview", json={"ref": ETA_REF, "version": "0.1.0"})
    assert preview["required_env"] == [], preview
    assert preview["missing_env"] == [], preview
    assert preview["delivery"] == "package", preview
