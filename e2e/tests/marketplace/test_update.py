"""Skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Update in place: install alpha 0.1.0, confirm the registry advertises 0.2.0, then
update and prove the SAME probe tool answers 0.2.0 from the live process and the
attribution store follows. An update of an mcp-server plugin keeps the tool pick its
manifest ``mcp`` entry carries.
"""

from __future__ import annotations

import pytest

from tai42_e2e.catalog_seed import publish_eta_v2, seed_eta_listing
from tai42_e2e.fixture_catalog import (
    ALPHA_PACKAGE,
    ALPHA_REF,
    ETA_MCP_TITLE,
    ETA_MCP_TOOL,
    ETA_PACKAGE,
    ETA_REF,
    FixtureArtifacts,
)
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._market_support import (
    MarketInstaller,
    installed_refs,
    persisted_manifest,
    uninstall_and_assert_clean,
    wait_tool_live,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "mutable",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
        "setting:PIP_INDEX_URL",
        "setting:shared-venv",
        "setting:fixture-plugin:alpha",
    ),
]

_TOOL = "e2e_market_probe"


async def test_update_lands_new_version(marketplace_service: MarketplaceService, marketplace_stack: TaiStack) -> None:
    stack = marketplace_stack

    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    row = (await installed_refs(stack))[ALPHA_REF]
    assert row["version"] == "0.1.0"
    assert row["update_available"] is True

    await stack.api().post("/api/marketplace/update", json={"ref": ALPHA_REF})
    await wait_tool_live(stack, _TOOL, dist_version="0.2.0")

    async def store_reads_new_version() -> bool:
        updated = (await installed_refs(stack)).get(ALPHA_REF)
        return updated is not None and updated["version"] == "0.2.0" and updated["update_available"] is False

    await wait_for_async(
        store_reads_new_version,
        deadline=30.0,
        message="attribution store never reported the updated version 0.2.0",
    )

    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)


def _eta_entry(stack: TaiStack) -> dict:
    return next(entry for entry in persisted_manifest(stack)["mcp"] if entry["title"] == ETA_MCP_TITLE)


@pytest.mark.needs("setting:fixture-plugin:eta", "files")
async def test_update_keeps_an_mcp_entrys_tool_pick(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    marketplace_stack: TaiStack,
    market_installer: MarketInstaller,
) -> None:
    stack = marketplace_stack
    await seed_eta_listing(marketplace_service, package_index, fixture_artifacts)
    await publish_eta_v2(marketplace_service, package_index, fixture_artifacts)

    await market_installer.install(stack, ETA_REF, ETA_PACKAGE, version="0.1.0")

    async def bound() -> bool:
        status = await stack.api().get("/api/mcp-status")
        return ETA_MCP_TOOL in (status["bound"].get(ETA_MCP_TITLE) or [])

    await wait_for_async(bound, deadline=45.0, message=f"{ETA_MCP_TOOL!r} never bound after the install")

    await stack.api().put(
        f"/api/mcp-config/entries/{ETA_MCP_TITLE}/tool-filter",
        json={"include": [ETA_MCP_TOOL], "exclude": []},
        retry_on_reloading=True,
    )
    assert _eta_entry(stack)["include"] == [ETA_MCP_TOOL]

    await stack.api().post("/api/marketplace/update", json={"ref": ETA_REF})

    async def updated() -> bool:
        row = (await installed_refs(stack)).get(ETA_REF)
        return row is not None and row["version"] == "0.2.0"

    await wait_for_async(updated, deadline=30.0, message="attribution store never reported eta 0.2.0")
    entry = _eta_entry(stack)
    assert (entry.get("include", []), entry.get("exclude", [])) == ([ETA_MCP_TOOL], []), entry
    await wait_for_async(bound, deadline=45.0, message=f"{ETA_MCP_TOOL!r} never bound after the update")

    await uninstall_and_assert_clean(
        stack, ETA_REF, package=ETA_PACKAGE, tool_name=ETA_MCP_TOOL, mcp_title=ETA_MCP_TITLE
    )
