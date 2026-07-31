"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

upgrade-all, contract-aware: with alpha installed at 0.1.0 (0.2.0 published and
compatible) and zeta installed at 0.1.0 (0.2.0 published but declaring a narrow
future contract range), one sweep upgrades alpha in place and leaves zeta
untouched, reporting a per-ref outcome for both — ``upgraded`` for the move,
``up-to-date`` with the blocked newer version named for the install whose only
newer version needs a newer core. The HTTP door and the ``tai plugins upgrade
--all`` CLI drive the same path, each proven on the live process (the SAME
probe tools answer the expected dist versions after the sweep) and ending
clean. The ``no-compatible-version`` outcome is exercised by the quarantine
spec, whose registry publishes no compatible version at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _market_support import (
    cli_env,
    installed_refs,
    ok_json,
    outcomes_by_ref,
    run_cli,
    uninstall_and_assert_clean,
    wait_tool_live,
)

from tai42_e2e.marketplace import (
    ALPHA_PACKAGE,
    ALPHA_REF,
    ZETA_COMPAT_VERSION,
    ZETA_INCOMPAT_VERSION,
    ZETA_PACKAGE,
    ZETA_REF,
    MarketplaceService,
)
from tai42_e2e.pkgsource import BuiltWheel
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.backendless

_ALPHA_TOOL = "e2e_market_probe"
_ZETA_TOOL = "e2e_zeta_probe"


async def _install_both_at_oldest(stack: TaiStack) -> None:
    """Install alpha 0.1.0 (outdated, 0.2.0 compatible) and zeta 0.1.0
    (outdated, 0.2.0 contract-incompatible), each proven live."""
    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})
    await wait_tool_live(stack, _ALPHA_TOOL, dist_version="0.1.0")
    await stack.api().post("/api/marketplace/install", json={"ref": ZETA_REF, "version": ZETA_COMPAT_VERSION})
    await wait_tool_live(stack, _ZETA_TOOL, dist_version=ZETA_COMPAT_VERSION)


async def _assert_compat_aware_update_picture(stack: TaiStack) -> None:
    """The pre-sweep inventory picture: alpha advertises its compatible update;
    zeta does NOT (its newer version is incompatible and is surfaced as
    ``incompatible_newer``, never as an available update)."""
    rows = await installed_refs(stack)
    assert rows[ALPHA_REF]["update_available"] is True
    assert rows[ALPHA_REF]["latest"] == "0.2.0"
    assert rows[ZETA_REF]["update_available"] is False
    assert rows[ZETA_REF]["incompatible_newer"] == ZETA_INCOMPAT_VERSION


async def _assert_upgrade_all_outcome(stack: TaiStack, payload: object) -> None:
    """The sweep's contract, asserted on the response AND the live process:
    alpha upgraded to 0.2.0; zeta reported up-to-date with the blocked newer
    version named, and untouched."""
    outcomes = outcomes_by_ref(payload)
    assert set(outcomes) == {ALPHA_REF, ZETA_REF}
    assert outcomes[ALPHA_REF]["outcome"] == "upgraded"
    assert "0.2.0" in outcomes[ALPHA_REF]["detail"]
    assert outcomes[ZETA_REF]["outcome"] == "up-to-date"
    # The blocked newer version is named, never silently omitted.
    assert ZETA_INCOMPAT_VERSION in outcomes[ZETA_REF]["detail"]

    # The SAME live process serves the upgraded alpha wheel; zeta still serves
    # its 0.1.0 — passed over, not uninstalled, not broken.
    await wait_tool_live(stack, _ALPHA_TOOL, dist_version="0.2.0")
    await wait_tool_live(stack, _ZETA_TOOL, dist_version=ZETA_COMPAT_VERSION)

    async def store_settled() -> bool:
        rows = await installed_refs(stack)
        alpha = rows.get(ALPHA_REF)
        zeta = rows.get(ZETA_REF)
        if alpha is None or zeta is None:
            return False
        return alpha["version"] == "0.2.0" and zeta["version"] == ZETA_COMPAT_VERSION

    await wait_for_async(
        store_settled,
        deadline=30.0,
        message="attribution store never settled on alpha 0.2.0 + zeta 0.1.0 after upgrade-all",
    )


async def _uninstall_both(stack: TaiStack) -> None:
    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_ALPHA_TOOL)
    await uninstall_and_assert_clean(stack, ZETA_REF, package=ZETA_PACKAGE, tool_name=_ZETA_TOOL)


async def test_upgrade_all_upgrades_compatible_and_reports_per_ref(
    marketplace_service: MarketplaceService,
    marketplace_stack: TaiStack,
    zeta_catalog: tuple[BuiltWheel, BuiltWheel],
) -> None:
    stack = marketplace_stack
    await _install_both_at_oldest(stack)
    await _assert_compat_aware_update_picture(stack)

    payload = await stack.api().post("/api/marketplace/upgrade-all")
    await _assert_upgrade_all_outcome(stack, payload)

    assert (await installed_refs(stack))[ALPHA_REF]["update_available"] is False
    await _uninstall_both(stack)


async def test_cli_upgrade_all_drives_the_same_path(
    marketplace_service: MarketplaceService,
    marketplace_stack: TaiStack,
    zeta_catalog: tuple[BuiltWheel, BuiltWheel],
    tmp_path: Path,
) -> None:
    stack = marketplace_stack
    env = cli_env(stack, tmp_path)
    await _install_both_at_oldest(stack)

    payload = ok_json(run_cli(env, "upgrade", "--all"))
    await _assert_upgrade_all_outcome(stack, payload)

    installed = {row["ref"]: row for row in ok_json(run_cli(env, "installed"))["installed"]}
    assert installed[ALPHA_REF]["version"] == "0.2.0"
    assert installed[ZETA_REF]["version"] == ZETA_COMPAT_VERSION
    await _uninstall_both(stack)
