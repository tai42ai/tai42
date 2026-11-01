"""Skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

A core upgrade strands an installed plugin whose declared contract range excludes the
running contract: a core upgrade leaves the layer holding it; boot refuses until
``tai plugins rebuild``, which leaves the plugin out of the layer and reports it; the
inventory then shows it incompatible, and upgrade-all reports the honest
``no-compatible-version`` outcome — this module's registry publishes ONLY the narrow-range
zeta 0.2.0, so no published version supports the running core.

Boot abort: a skeleton whose manifest names a module of an installed plugin the rebuilt
layer left out cannot load it, so boot aborts — the process exits non-zero naming the
module, its kind, and the reason, rather than coming up missing a plugin the manifest
asked for.

The stranded state is forged exactly the way a core upgrade strands a real install — no
API can create it, because install/update correctly refuse an incompatible pin and a build
of the plugin layer leaves it out: the zeta 0.2.0 wheel (narrow future contract range) is
laid into a plugin layer build that is made the stack's current one — the build a layer
made before the core upgrade leaves behind — and its attribution row is seeded into the
stack's own marketplace store; the boot-abort stack ALSO wires its tool module into the
manifest as the installer-shaped config row.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from tai42_skeleton.marketplace.environments.layout import BuildInput

from tai42_e2e import Infra, diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.catalog_seed import assert_zeta_ranges_bracket_running_contract, seed_zeta_listing
from tai42_e2e.fixture_catalog import ZETA_INCOMPAT_VERSION, ZETA_PACKAGE, ZETA_REF, ZETA_TOOLS_MODULE
from tai42_e2e.manifests import build_marketplace_quarantine_stack, build_marketplace_stack
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex
from tai42_e2e.plugin_layer import lay_wheel_as_current_layer, record_install_row
from tai42_e2e.stack import TaiStack

from ._market_support import (
    compat_block,
    installed_payload,
    installed_refs,
    layer_holds,
    outcomes_by_ref,
    run_rebuild,
    stack_plugins_dir,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "store:postgres",
        "files",
        "process",
        "cli",
        "helper:registry",
        "helper:package-index",
        "setting:fixture-plugin:zeta",
        "setting:TAI_PLUGINS_DIR",
    ),
]


@dataclass(frozen=True)
class StrandRecovery:
    """A stranded stack taken through its recovery: the serve start's refusal, the rebuild, the start."""

    stack: TaiStack
    refusal: str | None
    rebuild: subprocess.CompletedProcess[str]


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


def _forge_strand(stack: TaiStack, narrow_wheel: BuiltWheel) -> None:
    """Lay the narrow zeta wheel as ``stack``'s current plugin layer build and seed its
    attribution row — the layer and the store a core upgrade leaves behind."""
    # The narrow-range wheel's lockstep ``Requires-Dist`` names a tai42-contract line
    # neither the image nor the fixture index serves, so only its own distribution is laid.
    built_from = BuildInput(
        ref=ZETA_REF, package=ZETA_PACKAGE, version=narrow_wheel.version, source="pypi", sha256=None
    )
    lay_wheel_as_current_layer(stack_plugins_dir(stack), narrow_wheel.path, built_from)
    record_install_row(stack.resources, built_from, narrow_wheel.plugin_yml)


def _rebuilt(stack: TaiStack, home: Path) -> subprocess.CompletedProcess[str]:
    """Run ``tai plugins rebuild`` for ``stack``, raising with its output when it fails."""
    rebuilt = run_rebuild(stack, home)
    if rebuilt.returncode != 0:
        raise RuntimeError(
            f"tai plugins rebuild failed (exit {rebuilt.returncode}): {rebuilt.stdout}\n{rebuilt.stderr}"
        )
    return rebuilt


def _resource_kwargs(marketplace_service: MarketplaceService, package_index: FixturePackageIndex) -> dict[str, str]:
    return {"marketplace_url": marketplace_service.base_url, "package_index_url": package_index.url}


@pytest.fixture
def quarantine_boot_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    narrow_only_zeta_registry: BuiltWheel,
) -> Iterator[TaiStack]:
    """The marketplace stack that ALSO wires zeta's tool module into the manifest, forged into
    the strand state and rebuilt, but NOT booted: the test boots it and asserts the abort."""
    root = tmp_path_factory.mktemp("marketplace-quarantine")
    resources, config = allocate_and_build(
        infra,
        root,
        build_marketplace_quarantine_stack,
        _resource_kwargs(marketplace_service, package_index),
        False,
    )
    stack = TaiStack(config, infra, resources, root)
    try:
        _forge_strand(stack, narrow_only_zeta_registry)
        stack.render_config()
        _rebuilt(stack, tmp_path_factory.mktemp("quarantine-cli-home"))
        yield stack
    finally:
        stack.teardown()


@pytest.fixture(scope="module")
def strand_recovery(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    narrow_only_zeta_registry: BuiltWheel,
) -> Iterator[StrandRecovery]:
    """The marketplace stack (zeta NOT wired into the manifest) stranded by a core upgrade and
    recovered: zeta's layer and row are forged under the running stack, a fresh serve start
    is attempted, ``tai plugins rebuild`` runs, and serve starts again."""
    root = tmp_path_factory.mktemp("marketplace-stranded")
    resources, config = allocate_and_build(
        infra, root, build_marketplace_stack, _resource_kwargs(marketplace_service, package_index), False
    )
    stack = TaiStack(config, infra, resources, root)
    with stack, diagnostics.track(stack):
        _forge_strand(stack, narrow_only_zeta_registry)
        refusal: str | None = None
        try:
            stack.restart("serve")
        except RuntimeError as exc:
            refusal = str(exc)
        rebuilt = _rebuilt(stack, tmp_path_factory.mktemp("stranded-cli-home"))
        stack.restart("serve")
        yield StrandRecovery(stack=stack, refusal=refusal, rebuild=rebuilt)


@pytest.mark.needs("kind:marketplace_store", "setting:MARKETPLACE_URL")
async def test_a_core_upgrade_strands_the_plugin_until_rebuild(strand_recovery: StrandRecovery) -> None:
    # The layer a core upgrade leaves behind still holds zeta, whose declared range this
    # image's contract is outside, so a fresh serve process refuses to start and names the rebuild.
    refusal = strand_recovery.refusal
    assert refusal is not None, "a serve start over the stranded layer was not refused"
    assert "plugin environment is out of date" in refusal, refusal
    assert ZETA_REF in refusal, refusal
    assert "does not support" in refusal, refusal

    # The rebuild leaves zeta out of the layer and says so on the layer's row.
    stdout = strand_recovery.rebuild.stdout
    assert "rebuilt" in stdout, stdout
    assert f"left out {ZETA_REF} {ZETA_INCOMPAT_VERSION}" in stdout, stdout

    # The stack then starts and serves, with zeta still recorded but not in the layer.
    stack = strand_recovery.stack
    assert not layer_holds(stack, ZETA_PACKAGE)
    assert ZETA_REF in await installed_refs(stack)


async def test_incompatible_installed_manifest_plugin_aborts_boot(quarantine_boot_stack: TaiStack) -> None:
    # The manifest names zeta's tool module, whose declared contract range excludes the
    # running contract, so boot cannot load it and aborts — the process exits early and the
    # readiness wait raises with the child's boot-abort detail, naming the module, its kind,
    # and the reason. The rebuilt layer left zeta out; a booting server would be one silently
    # missing a plugin its manifest asked for.
    with pytest.raises(RuntimeError, match=re.escape(ZETA_TOOLS_MODULE)) as excinfo:
        quarantine_boot_stack.boot()
    message = str(excinfo.value)
    assert "incompatible" in message, message


@pytest.mark.needs("kind:marketplace_store", "setting:MARKETPLACE_URL")
async def test_upgrade_all_reports_no_compatible_version(strand_recovery: StrandRecovery) -> None:
    # zeta is installed and attributed but not manifest-wired, so the rebuilt stack boots with
    # the row stranded: the inventory shows it incompatible, and the upgrade sweep cannot fix it
    # — no published zeta version supports the running contract in this module's registry —
    # so it says so per ref instead of failing the batch or silently skipping the row.
    stranded_stack = strand_recovery.stack
    before = await installed_payload(stranded_stack)
    row = {r["ref"]: r for r in before["installed"]}[ZETA_REF]
    assert row["version"] == ZETA_INCOMPAT_VERSION
    compat = compat_block(row)
    assert compat["status"] == "incompatible"
    assert compat["reason"]

    payload = await stranded_stack.api().post("/api/marketplace/upgrade-all")
    outcomes = outcomes_by_ref(payload)
    assert set(outcomes) == {ZETA_REF}
    assert outcomes[ZETA_REF]["outcome"] == "no-compatible-version"
    assert outcomes[ZETA_REF]["detail"]

    # The sweep touched nothing: the row and its incompatible verdict are unchanged.
    after = await installed_payload(stranded_stack)
    after_row = {r["ref"]: r for r in after["installed"]}[ZETA_REF]
    assert after_row["version"] == ZETA_INCOMPAT_VERSION
    assert compat_block(after_row)["status"] == "incompatible"
