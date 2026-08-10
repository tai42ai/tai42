"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Update in place: install alpha 0.1.0, confirm the registry advertises 0.2.0, then
update and prove the SAME probe tool answers 0.2.0 from the live process and the
attribution store follows.
"""

from __future__ import annotations

import pytest
from _market_support import installed_refs, uninstall_and_assert_clean, wait_tool_live

from tai42_e2e.marketplace import ALPHA_PACKAGE, ALPHA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.backendless

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
