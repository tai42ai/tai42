"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

``tai plugins`` covers-parity: the real venv ``tai`` binary drives the same
``/api/marketplace/*`` surface the HTTP specs use, from browse through the full
install → advisory → update → uninstall lifecycle, ending clean.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from _market_support import (
    cli_env,
    distribution_absent,
    installed_refs,
    ok_json,
    run_cli,
    wait_tool_absent,
    wait_tool_live,
)

from tai42_e2e.marketplace import ALPHA_PACKAGE, ALPHA_REF, BETA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.backendless

_TOOL = "e2e_market_probe"


async def test_cli_plugins_parity(
    marketplace_service: MarketplaceService,
    marketplace_stack: TaiStack,
    uniq: Callable[[str], str],
    tmp_path: Path,
) -> None:
    mp = marketplace_service
    stack = marketplace_stack
    env = cli_env(stack, tmp_path)

    # Browse.
    search = ok_json(run_cli(env, "search", "probe"))
    assert ALPHA_REF in {row["ref"] for row in search["listings"]}

    faceted = ok_json(run_cli(env, "search", "probe", "--kind", "tool", "--tag", "alpha", "--sort", "name"))
    faceted_refs = {row["ref"] for row in faceted["listings"]}
    assert ALPHA_REF in faceted_refs
    assert BETA_REF not in faceted_refs

    categories = ok_json(run_cli(env, "categories"))
    assert "utilities" in categories

    info = ok_json(run_cli(env, "info", ALPHA_REF))
    assert info["latest"]["version"] == "0.2.0"

    # Install (pinned) and confirm the tool goes live.
    ok_json(run_cli(env, "install", ALPHA_REF, "--version", "0.1.0"))
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")
    installed = {row["ref"]: row for row in ok_json(run_cli(env, "installed"))["installed"]}
    assert installed[ALPHA_REF]["version"] == "0.1.0"

    # Advisory honesty: create, wait it propagates, CLI surfaces it, withdraw, clears.
    summary = uniq("advisory")
    created = await mp.api.post(
        "/api/v1/admin/advisories",
        json={"listing": ALPHA_REF, "affected_versions": "<=0.1.0", "severity": "high", "summary": summary},
        headers=mp.admin_headers,
        expect=201,
    )
    await wait_for_async(
        lambda: _api_advisory_present(stack, summary),
        deadline=15.0,
        message=f"skeleton never surfaced advisory {summary!r}",
    )
    cli_advisories = ok_json(run_cli(env, "advisories"))
    assert summary in {row["summary"] for row in cli_advisories["advisories"]}

    await mp.api.post(f"/api/v1/admin/advisories/{created['id']}/withdraw", headers=mp.admin_headers)
    await wait_for_async(
        lambda: _api_advisory_absent(stack, summary),
        deadline=15.0,
        message=f"skeleton never cleared withdrawn advisory {summary!r}",
    )
    cleared = ok_json(run_cli(env, "advisories"))
    assert summary not in {row["summary"] for row in cleared["advisories"]}

    # Update.
    ok_json(run_cli(env, "update", ALPHA_REF))
    await wait_tool_live(stack, _TOOL, dist_version="0.2.0")
    updated = {row["ref"]: row for row in ok_json(run_cli(env, "installed"))["installed"]}
    assert updated[ALPHA_REF]["version"] == "0.2.0"

    # Uninstall + clean tail.
    ok_json(run_cli(env, "uninstall", ALPHA_REF))
    await wait_tool_absent(stack, _TOOL)

    async def gone() -> bool:
        if ALPHA_REF in await installed_refs(stack):
            return False
        return distribution_absent(ALPHA_PACKAGE)

    await wait_for_async(gone, deadline=30.0, message=f"{ALPHA_REF} never fully uninstalled via the CLI")

    # Failure honesty: an unknown ref exits non-zero and names the ref on stderr.
    failure = run_cli(env, "info", "tai42/nope")
    assert failure.returncode != 0
    assert "tai42/nope" in (failure.stdout + failure.stderr)


async def _api_advisory_present(stack: TaiStack, summary: str) -> bool:
    snapshot = await stack.api().get("/api/marketplace/advisories")
    return summary in {row["summary"] for row in snapshot["advisories"]}


async def _api_advisory_absent(stack: TaiStack, summary: str) -> bool:
    return not await _api_advisory_present(stack, summary)
