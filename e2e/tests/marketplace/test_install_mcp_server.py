"""C7 — installing an ``mcp-server``-kind plugin item, end to end. Opt-in: collects
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

This leg requires the marketplace registry's mcp-server ingest branch to be present,
so the contract-less fixture seeds and its marketplace pin resolves at registry
install. It is part of the ``TAI_E2E_MARKETPLACE``-gated suite (collected only with
``TAI_E2E_MARKETPLACE=1``).
"""

from __future__ import annotations

import pytest
from _market_support import (
    api_tool_names,
    distribution_absent,
    installed_refs,
    manifest_mcp_titles,
    probe_payload,
    uninstall_and_assert_clean,
)

from tai42_e2e import wait_for_async
from tai42_e2e.marketplace import (
    ETA_MCP_TITLE,
    ETA_MCP_TOOL,
    ETA_PACKAGE,
    ETA_REF,
    FixtureArtifacts,
    MarketplaceService,
    seed_eta_listing,
)
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


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
