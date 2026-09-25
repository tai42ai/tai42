"""Skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Boot abort: a skeleton whose manifest names an INSTALLED plugin whose declared
contract range excludes the running contract cannot load it, so boot aborts —
the process exits non-zero naming the module, its kind, and the reason, rather
than coming up missing a plugin the manifest asked for.

Upgrade-all over a stranded install: an installed-but-not-manifest-wired plugin
whose declared range excludes the running contract shows in the installed
inventory with an incompatible ``compat.status``, and upgrade-all reports the
honest ``no-compatible-version`` outcome — this module's registry publishes ONLY
the narrow-range zeta 0.2.0, so no published version supports the running core.

The stranded state is forged before boot, exactly the way a core upgrade strands
a real install — no API can create it, because install/update correctly refuse an
incompatible pin: the zeta 0.2.0 wheel (narrow future contract range) is installed
into the stack's plugin prefix and its attribution row is seeded into the stack's
own marketplace store; the boot-abort stack ALSO wires its tool module into the
manifest as the installer-shaped config row.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg.types.json import Json

from tai42_e2e import Infra, StackResources, diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.catalog_seed import assert_zeta_ranges_bracket_running_contract, seed_zeta_listing
from tai42_e2e.fixture_catalog import ZETA_INCOMPAT_VERSION, ZETA_REF, ZETA_TOOLS_MODULE
from tai42_e2e.manifests import build_marketplace_prefix_stack, build_marketplace_quarantine_stack
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex
from tai42_e2e.stack import StackConfig, TaiStack

from ._market_support import (
    compat_block,
    installed_payload,
    outcomes_by_ref,
)

pytestmark = pytest.mark.backendless


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
    both the strand story and upgrade-all's ``no-compatible-version``
    scenario. Returns the narrow wheel."""
    assert_zeta_ranges_bracket_running_contract()
    _wide_wheel, narrow_wheel = zeta_compat_wheels
    asyncio.run(seed_zeta_listing(marketplace_service, package_index, [narrow_wheel]))
    return narrow_wheel


def _forge_stranded_stack(
    infra: Infra,
    root: Path,
    build: Callable[..., StackConfig],
    resource_kwargs: dict[str, str],
    narrow_wheel: BuiltWheel,
) -> TaiStack:
    """Build a stack via ``build``, install the narrow zeta wheel into its prefix, and
    seed its attribution row — the un-booted stack forged into the strand state."""
    resources, config = allocate_and_build(infra, root, build, resource_kwargs, False)
    stack = TaiStack(config, infra, resources, root)
    try:
        _install_wheel_into_prefix(config.env["TAI_PLUGINS_PREFIX"], narrow_wheel)
        _seed_install_row(resources, narrow_wheel)
    except BaseException:
        stack.teardown()
        raise
    return stack


@pytest.fixture
def quarantine_boot_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    narrow_only_zeta_registry: BuiltWheel,
) -> Iterator[TaiStack]:
    """The marketplace-prefix stack that ALSO wires zeta's tool module into the manifest,
    forged into the strand state but NOT booted: the test boots it and asserts the abort."""
    root = tmp_path_factory.mktemp("marketplace-quarantine")
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
    }
    stack = _forge_stranded_stack(
        infra, root, build_marketplace_quarantine_stack, resource_kwargs, narrow_only_zeta_registry
    )
    try:
        yield stack
    finally:
        stack.teardown()


@pytest.fixture(scope="module")
def stranded_prefix_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    narrow_only_zeta_registry: BuiltWheel,
) -> Iterator[TaiStack]:
    """The marketplace-prefix stack with zeta installed at its narrow-range 0.2.0 and
    attributed in the store, but NOT wired into the manifest — so it boots (nothing
    imports the incompatible module) with the row stranded in the inventory."""
    root = tmp_path_factory.mktemp("marketplace-stranded")
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
    }
    stack = _forge_stranded_stack(
        infra, root, build_marketplace_prefix_stack, resource_kwargs, narrow_only_zeta_registry
    )
    with stack, diagnostics.track(stack):
        yield stack


async def test_incompatible_installed_manifest_plugin_aborts_boot(quarantine_boot_stack: TaiStack) -> None:
    # The manifest names zeta's tool module, whose declared contract range excludes the
    # running contract, so boot cannot load it and aborts — the process exits early and the
    # readiness wait raises with the child's boot-abort detail, naming the module, its kind,
    # and the reason. The forged state is exactly what a core upgrade strands a real install
    # in; a booting server would be one silently missing a plugin its manifest asked for.
    with pytest.raises(RuntimeError, match=re.escape(ZETA_TOOLS_MODULE)) as excinfo:
        quarantine_boot_stack.boot()
    message = str(excinfo.value)
    assert "incompatible" in message, message


async def test_upgrade_all_reports_no_compatible_version(stranded_prefix_stack: TaiStack) -> None:
    # zeta is installed and attributed but not manifest-wired, so the stack boots with the
    # row stranded: the inventory shows it incompatible, and the upgrade sweep cannot fix it
    # — no published zeta version supports the running contract in this module's registry —
    # so it says so per ref instead of failing the batch or silently skipping the row.
    before = await installed_payload(stranded_prefix_stack)
    row = {r["ref"]: r for r in before["installed"]}[ZETA_REF]
    assert row["version"] == ZETA_INCOMPAT_VERSION
    compat = compat_block(row)
    assert compat["status"] == "incompatible"
    assert compat["reason"]

    payload = await stranded_prefix_stack.api().post("/api/marketplace/upgrade-all")
    outcomes = outcomes_by_ref(payload)
    assert set(outcomes) == {ZETA_REF}
    assert outcomes[ZETA_REF]["outcome"] == "no-compatible-version"
    assert outcomes[ZETA_REF]["detail"]

    # The sweep touched nothing: the row and its incompatible verdict are unchanged.
    after = await installed_payload(stranded_prefix_stack)
    after_row = {r["ref"]: r for r in after["installed"]}[ZETA_REF]
    assert after_row["version"] == ZETA_INCOMPAT_VERSION
    assert compat_block(after_row)["status"] == "incompatible"
