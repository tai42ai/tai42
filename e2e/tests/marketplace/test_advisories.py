"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The advisory round-trip: an admin-created advisory against the installed version
propagates to the skeleton's cached snapshot within the short poll, and a
withdrawal clears it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _market_support import uninstall_and_assert_clean, wait_tool_live

from tai42_e2e.marketplace import ALPHA_PACKAGE, ALPHA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.backendless

_TOOL = "e2e_market_probe"


async def _skeleton_summaries(stack: TaiStack) -> set[str]:
    snapshot = await stack.api().get("/api/marketplace/advisories")
    return {row["summary"] for row in snapshot["advisories"]}


async def test_advisory_propagates_and_withdraws(
    marketplace_service: MarketplaceService, marketplace_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    mp = marketplace_service
    stack = marketplace_stack
    summary = uniq("advisory")

    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    created = await mp.api.post(
        "/api/v1/admin/advisories",
        json={"listing": ALPHA_REF, "affected_versions": "<=0.1.0", "severity": "high", "summary": summary},
        headers=mp.admin_headers,
        expect=201,
    )
    advisory_id = created["id"]

    # The registry surfaces it on its own read within the short poll (the create
    # is not guaranteed read-your-write on the separate advisories read path).
    await wait_for_async(
        lambda: _registry_has_summary(mp, summary),
        deadline=15.0,
        message=f"registry advisory list never surfaced {summary!r}",
    )

    # The skeleton surfaces it for the installed plugin within the 1s poll.
    await wait_for_async(
        lambda: _summary_present(stack, summary),
        deadline=15.0,
        message=f"skeleton advisory snapshot never surfaced {summary!r}",
    )

    await mp.api.post(f"/api/v1/admin/advisories/{advisory_id}/withdraw", headers=mp.admin_headers)

    await wait_for_async(
        lambda: _summary_absent(stack, summary),
        deadline=15.0,
        message=f"skeleton advisory snapshot never cleared withdrawn {summary!r}",
    )

    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)


async def _summary_present(stack: TaiStack, summary: str) -> bool:
    return summary in await _skeleton_summaries(stack)


async def _summary_absent(stack: TaiStack, summary: str) -> bool:
    return summary not in await _skeleton_summaries(stack)


async def _registry_has_summary(mp: MarketplaceService, summary: str) -> bool:
    rows = await mp.api.get(f"/api/v1/advisories?listing={ALPHA_REF}")
    return summary in {row["summary"] for row in rows["advisories"]}
