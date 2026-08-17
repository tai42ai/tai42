"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

``tai plugins`` covers-parity: the real venv ``tai`` binary drives the same
``/api/marketplace/*`` surface the HTTP specs use, from browse through the full
install → advisory → update → uninstall lifecycle, ending clean.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _market_support import (
    cli_env,
    distribution_absent,
    installed_refs,
    ok_json,
    run_cli,
    tai_bin,
    wait_tool_absent,
    wait_tool_live,
)

from tai42_e2e.marketplace import (
    ALPHA_PACKAGE,
    ALPHA_REF,
    BETA_REF,
    EPSILON_PACKAGE,
    EPSILON_REF,
    THETA_PACKAGE,
    THETA_REF,
    FixtureArtifacts,
    MarketplaceService,
    seed_epsilon_listing,
    seed_theta_listing,
)
from tai42_e2e.pkgsource import FixturePackageIndex
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


# The epsilon fixture's route-carrying item name (its declared mount base is remapped
# by ``--mount item=base``).
_EPSILON_ITEM = "e2e_epsilon_router"


def _run_cli_plain(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``tai plugins <args>`` WITHOUT ``--json`` so the human table renders — the
    ``--dry-run`` preview prints its route table only in the non-json form."""
    return subprocess.run(
        [tai_bin(), "plugins", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )


async def test_cli_plugins_route_mounting(
    marketplace_service: MarketplaceService,
    marketplace_stack: TaiStack,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    tmp_path: Path,
) -> None:
    """The ``tai plugins install`` route-mounting flags: ``--dry-run`` prints the
    resolved-route table (with ``--mount`` remapping the base), the install refuses a
    declared public route without ``--accept-public-routes``, and accepts it with the
    flag — ending clean."""
    stack = marketplace_stack
    env = cli_env(stack, tmp_path)

    # Epsilon is kept out of the shared browse catalog; this leg seeds the listing it drives.
    await seed_epsilon_listing(marketplace_service, package_index, fixture_artifacts)

    # --dry-run at the default base: the table lists every resolved route with its auth
    # posture and warns that the public route needs acceptance (a preview, no install).
    dry = _run_cli_plain(env, "install", EPSILON_REF, "--version", "0.1.0", "--dry-run")
    assert dry.returncode == 0, f"dry-run exited non-zero: {dry.stdout}\n{dry.stderr}"
    out = dry.stdout
    assert f"item {_EPSILON_ITEM} (base e2e-epsilon):" in out, out
    assert "/api/e2e-epsilon/ping [authed]" in out, out
    assert "/api/e2e-epsilon/open [public]" in out, out
    # The table renders the HTTP method(s) column beside each resolved path.
    assert "GET /api/e2e-epsilon/ping [authed]" in out, out
    assert "pass --accept-public-routes" in out, out
    assert distribution_absent(EPSILON_PACKAGE), "dry-run must not install"

    # --dry-run with --mount: the table resolves the routes under the remapped base.
    remapped = _run_cli_plain(
        env, "install", EPSILON_REF, "--version", "0.1.0", "--mount", f"{_EPSILON_ITEM}=e2e-epsilon-cli", "--dry-run"
    )
    assert remapped.returncode == 0, f"remapped dry-run exited non-zero: {remapped.stdout}\n{remapped.stderr}"
    assert f"item {_EPSILON_ITEM} (base e2e-epsilon-cli):" in remapped.stdout, remapped.stdout
    assert "/api/e2e-epsilon-cli/ping [authed]" in remapped.stdout, remapped.stdout

    # Install WITHOUT --accept-public-routes: the declared public route refuses the install
    # (a non-zero exit; the same 400 semantics the HTTP door enforces).
    refused = run_cli(env, "install", EPSILON_REF, "--version", "0.1.0")
    assert refused.returncode != 0, f"install without acceptance should refuse: {refused.stdout}\n{refused.stderr}"
    assert "accept" in (refused.stdout + refused.stderr).lower(), refused.stdout + refused.stderr
    assert distribution_absent(EPSILON_PACKAGE), "a refused install must leave nothing behind"

    # Install WITH --accept-public-routes and a remapped base: the receipt carries the
    # mounted routes at the remapped base.
    installed = ok_json(
        run_cli(
            env,
            "install",
            EPSILON_REF,
            "--version",
            "0.1.0",
            "--mount",
            f"{_EPSILON_ITEM}=e2e-epsilon-cli",
            "--accept-public-routes",
        )
    )
    try:
        mounted = {row["full_path"] for row in installed["routes"]}
        assert mounted == {"/api/e2e-epsilon-cli/ping", "/api/e2e-epsilon-cli/open"}, installed["routes"]
    finally:
        ok_json(run_cli(env, "uninstall", EPSILON_REF))

    async def gone() -> bool:
        if EPSILON_REF in await installed_refs(stack):
            return False
        return distribution_absent(EPSILON_PACKAGE)

    await wait_for_async(gone, deadline=30.0, message=f"{EPSILON_REF} never fully uninstalled via the CLI")


async def test_cli_plugins_dry_run_collision(
    marketplace_service: MarketplaceService,
    marketplace_stack: TaiStack,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
    tmp_path: Path,
) -> None:
    """``tai plugins install --dry-run`` at a base that collides with an already
    installed plugin renders the collision row in the table AND exits NON-ZERO (so a
    scripted dry-run gates on it), without installing anything."""
    stack = marketplace_stack
    env = cli_env(stack, tmp_path)

    # Seed both listings this leg drives: epsilon (installed first) and theta (whose
    # default base shape-collides with it).
    await seed_epsilon_listing(marketplace_service, package_index, fixture_artifacts)
    await seed_theta_listing(marketplace_service, package_index, fixture_artifacts)

    # Install epsilon at its DEFAULT base so theta's default base collides with it.
    ok_json(run_cli(env, "install", EPSILON_REF, "--version", "0.1.0", "--accept-public-routes"))
    try:
        # --dry-run theta at its default base: the resolved route shape-collides with
        # epsilon's, so the table renders the collision row (method column, clashing
        # path, and the remap remedy) AND the command exits non-zero — without installing.
        dry = _run_cli_plain(env, "install", THETA_REF, "--version", "0.1.0", "--dry-run")
        assert dry.returncode != 0, f"a dry-run collision must exit non-zero: {dry.stdout}\n{dry.stderr}"
        out = dry.stdout
        assert "collision:" in out, out
        assert "GET /api/e2e-epsilon/{slug}" in out, out
        assert "remap the item base" in out, out
        assert distribution_absent(THETA_PACKAGE), "a dry-run must not install theta"
    finally:
        ok_json(run_cli(env, "uninstall", EPSILON_REF))

    async def gone() -> bool:
        if EPSILON_REF in await installed_refs(stack):
            return False
        return distribution_absent(EPSILON_PACKAGE)

    await wait_for_async(gone, deadline=30.0, message=f"{EPSILON_REF} never fully uninstalled via the CLI")
