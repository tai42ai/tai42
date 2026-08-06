"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Boot quarantine: a skeleton booted with an INSTALLED plugin whose declared
contract range excludes the running contract must boot healthy, must NOT
register the plugin's modules, and must show it in the installed inventory —
the row's ``compat.status`` incompatible, the module in the body's
``quarantined`` block. And upgrade-all over that state reports the honest
``no-compatible-version`` outcome: this module's registry publishes ONLY the
narrow-range zeta 0.2.0, so no published version supports the running core.

The stranded state is forged before boot, exactly the way a core upgrade
strands a real install — no API can create it, because install/update correctly
refuse an incompatible pin: the zeta 0.2.0 wheel (narrow future contract range)
is installed into the stack's plugin prefix, its tool module is wired into the
manifest as the installer-shaped config row, and its attribution row is seeded
into the stack's own marketplace store.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Iterator

import psycopg
import pytest
import yaml
from _market_support import (
    api_tool_names,
    compat_block,
    installed_payload,
    outcomes_by_ref,
    quarantined_by_name,
)
from psycopg.types.json import Json

from tai42_e2e import diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.manifests import build_marketplace_quarantine_stack
from tai42_e2e.marketplace import (
    ZETA_INCOMPAT_VERSION,
    ZETA_REF,
    ZETA_TOOLS_MODULE,
    MarketplaceService,
    assert_zeta_ranges_bracket_running_contract,
    seed_zeta_listing,
)
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex
from tai42_e2e.stack import Infra, StackResources, TaiStack

pytestmark = pytest.mark.backendless

_ZETA_TOOL = "e2e_zeta_probe"


def _install_wheel_into_prefix(prefix: str, wheel: BuiltWheel) -> None:
    """Land the wheel's own distribution under the plugin prefix, as a real
    install had left it.

    ``--no-deps`` because only the plugin's own distribution ever lands in the
    prefix (its dependencies were satisfied by the environment at install time),
    and the narrow-range wheel's lockstep ``Requires-Dist`` names a
    tai42-contract line the fixture index does not serve."""
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--prefix", prefix, str(wheel.path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install --prefix failed for {wheel.path.name} (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )


def _seed_install_row(resources: StackResources, wheel: BuiltWheel) -> None:
    """Insert the attribution row a real install of ``wheel`` had written, into
    the stack's own marketplace store (the per-stack PG clone the marketplace store
    binds to via the ``default`` database)."""
    spec = yaml.safe_load(wheel.plugin_yml)
    with (
        psycopg.connect(
            host=resources.pg_host,
            port=resources.pg_port,
            user=resources.pg_user,
            password=resources.pg_password,
            dbname=resources.pg_db,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO marketplace_installs (ref, version, source, spec) VALUES (%s, %s, %s, %s)",
            (ZETA_REF, wheel.version, "pypi", Json(spec)),
        )
        conn.commit()


@pytest.fixture(scope="module")
def narrow_only_zeta_registry(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    zeta_compat_wheels: tuple[BuiltWheel, BuiltWheel],
) -> BuiltWheel:
    """Publish ONLY the narrow-range zeta 0.2.0 into this module's registry —
    the listing has no contract-compatible published version at all, which is
    both the quarantine story and upgrade-all's ``no-compatible-version``
    scenario. Returns the narrow wheel."""
    assert_zeta_ranges_bracket_running_contract()
    _wide_wheel, narrow_wheel = zeta_compat_wheels
    asyncio.run(seed_zeta_listing(marketplace_service, package_index, [narrow_wheel]))
    return narrow_wheel


@pytest.fixture(scope="module")
def quarantine_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    narrow_only_zeta_registry: BuiltWheel,
) -> Iterator[TaiStack]:
    """The marketplace-prefix stack booted with zeta ALREADY installed at its
    narrow-range 0.2.0: distribution in the prefix, tool module in the manifest,
    attribution row in the store — and a declared contract range that excludes
    the running contract (the bracket is asserted by the registry fixture)."""
    narrow_wheel = narrow_only_zeta_registry
    root = tmp_path_factory.mktemp("marketplace-quarantine")
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
    }
    resources, config = allocate_and_build(infra, root, build_marketplace_quarantine_stack, resource_kwargs, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        _install_wheel_into_prefix(config.env["TAI_PLUGINS_PREFIX"], narrow_wheel)
        _seed_install_row(resources, narrow_wheel)
    except BaseException:
        stack.teardown()
        raise
    with stack, diagnostics.track(stack):
        yield stack


async def test_incompatible_installed_plugin_is_quarantined_at_boot(quarantine_stack: TaiStack) -> None:
    stack = quarantine_stack

    # The server booted healthy despite the incompatible install.
    health = await stack.api().request_raw("GET", "/health")
    assert health.status_code == 200, f"/health -> {health.status_code}: {health.text}"

    # The rest of the manifest registered normally — quarantine is per-plugin,
    # not a degraded boot — while the quarantined plugin's module did not.
    names = await api_tool_names(stack)
    assert "e2e_echo" in names
    assert _ZETA_TOOL not in names
    async with stack.mcp() as mcp:
        assert _ZETA_TOOL not in await mcp.tool_names()

    # The installed inventory still shows the row, marked incompatible, and the
    # body's quarantined block names the skipped module with a reason — the
    # operator can see WHAT is parked and why, instead of the plugin silently
    # vanishing.
    payload = await installed_payload(stack)
    row = {r["ref"]: r for r in payload["installed"]}[ZETA_REF]
    assert row["version"] == ZETA_INCOMPAT_VERSION
    compat = compat_block(row)
    assert compat["status"] == "incompatible"
    assert compat["reason"]
    quarantined = quarantined_by_name(payload)
    assert ZETA_TOOLS_MODULE in quarantined
    assert quarantined[ZETA_TOOLS_MODULE]


async def test_upgrade_all_reports_no_compatible_version(quarantine_stack: TaiStack) -> None:
    # No published zeta version supports the running contract in this module's
    # registry, so the sweep cannot fix the stranded install — it says so per
    # ref instead of failing the batch or silently skipping the row.
    payload = await quarantine_stack.api().post("/api/marketplace/upgrade-all")
    outcomes = outcomes_by_ref(payload)
    assert set(outcomes) == {ZETA_REF}
    assert outcomes[ZETA_REF]["outcome"] == "no-compatible-version"
    assert outcomes[ZETA_REF]["detail"]

    # The sweep touched nothing: the row and its quarantine state are unchanged.
    after = await installed_payload(quarantine_stack)
    row = {r["ref"]: r for r in after["installed"]}[ZETA_REF]
    assert row["version"] == ZETA_INCOMPAT_VERSION
    assert ZETA_TOOLS_MODULE in quarantined_by_name(after)
