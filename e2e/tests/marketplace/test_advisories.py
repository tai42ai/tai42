"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The advisory round-trip: an admin-created advisory against the installed version
propagates to the skeleton's cached snapshot within the short poll, and a
withdrawal clears it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.marketplace import ALPHA_PACKAGE, ALPHA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._market_support import MarketInstaller, uninstall_and_assert_clean, wait_tool_live

pytestmark = pytest.mark.backendless

_TOOL = "e2e_market_probe"

# The install POST runs pip install + a heavier build_and_swap reload; at the
# interleaved full-suite loaded tail it completes but exceeds the 60s default client read
# timeout, so this ONE slow install path is given a defensible ceiling (not a global slacken).
_INSTALL_TIMEOUT = 180.0


async def _skeleton_summaries(stack: TaiStack) -> set[str]:
    snapshot = await stack.api().get("/api/marketplace/advisories")
    return {row["summary"] for row in snapshot["advisories"]}


# 300s: the install POST's own read timeout is raised to 180s, so the default 120s test
# budget would be too tight if that call ran near its ceiling under interleaved load.
@pytest.mark.timeout(300)
async def test_advisory_propagates_and_withdraws(
    marketplace_service: MarketplaceService,
    marketplace_stack: TaiStack,
    uniq: Callable[[str], str],
    market_installer: MarketInstaller,
) -> None:
    mp = marketplace_service
    stack = marketplace_stack
    summary = uniq("advisory")

    await market_installer.install(stack, ALPHA_REF, ALPHA_PACKAGE, version="0.1.0", timeout=_INSTALL_TIMEOUT)
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
