"""The plugin layer resolves every in-process plugin together. Opt-in: collects only
with ``TAI_E2E_MARKETPLACE=1``.

Mu and nu are two in-process fixture plugins pinning the dependency-only
``tai-e2e-market-sharedlib`` (see ``SHAREDLIB_PACKAGE``). Every install rebuilds the
stack's one plugin layer from all its in-process plugins in one resolution, so:

* a conflicting pair is refused: with mu 0.1.0 installed (sharedlib below 2), nu 0.2.0
  (sharedlib 2 or above) answers 409 naming the conflict, and mu stays live from the
  layer it was serving from;
* a compatible pair shares one copy: mu 0.1.0 + nu 0.1.0 leave exactly one sharedlib
  distribution in the layer, at the version both admit.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from tai42_e2e.fixture_catalog import (
    MU_PACKAGE,
    MU_REF,
    MU_TOOL,
    NU_PACKAGE,
    NU_REF,
    NU_TOOL,
    SHAREDLIB_PACKAGE,
    SHAREDLIB_V1_VERSION,
)
from tai42_e2e.stack import TaiStack

from ._market_support import (
    MarketInstaller,
    image_holds,
    installed_refs,
    layer_holds,
    layer_site_packages,
    plugin_absent,
    uninstall_and_assert_clean,
    wait_tool_live,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "mutable",
        "files",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
        "setting:PIP_INDEX_URL",
        "setting:TAI_PLUGINS_DIR",
        "setting:fixture-plugin:mu",
        "setting:fixture-plugin:nu",
    ),
]


def _layer_versions(stack: TaiStack, package: str) -> list[str]:
    """Every version of ``package`` the stack's current plugin layer build holds, read from disk."""
    paths = [str(site) for site in layer_site_packages(stack)]
    return sorted(
        dist.version for dist in importlib.metadata.distributions(path=paths) if dist.metadata["Name"] == package
    )


async def test_a_conflicting_second_plugin_is_refused_and_the_first_stays_live(
    mu_nu_catalog: None, marketplace_stack: TaiStack, market_installer: MarketInstaller
) -> None:
    stack = marketplace_stack
    assert plugin_absent(stack, MU_PACKAGE)
    assert plugin_absent(stack, NU_PACKAGE)

    await market_installer.install(stack, MU_REF, MU_PACKAGE, version="0.1.0")
    await wait_tool_live(stack, MU_TOOL, dist_version="0.1.0")
    assert _layer_versions(stack, SHAREDLIB_PACKAGE) == [SHAREDLIB_V1_VERSION]

    # Nu 0.2.0 needs sharedlib 2 or above, which mu 0.1.0 excludes: the layer cannot hold
    # both, so the install is refused before any build is used.
    body = await stack.api().post("/api/marketplace/install", json={"ref": NU_REF, "version": "0.2.0"}, expect=409)
    assert NU_REF in body["error"], body
    assert "conflict" in body["error"], body

    # Nothing of nu landed, and mu still answers from the layer it was serving from.
    assert NU_REF not in await installed_refs(stack)
    assert plugin_absent(stack, NU_PACKAGE)
    assert _layer_versions(stack, SHAREDLIB_PACKAGE) == [SHAREDLIB_V1_VERSION]
    await wait_tool_live(stack, MU_TOOL, dist_version="0.1.0")

    await uninstall_and_assert_clean(stack, MU_REF, package=MU_PACKAGE, tool_name=MU_TOOL)
    assert not layer_holds(stack, SHAREDLIB_PACKAGE)


async def test_a_compatible_pair_shares_one_copy_of_their_dependency(
    mu_nu_catalog: None, marketplace_stack: TaiStack, market_installer: MarketInstaller
) -> None:
    stack = marketplace_stack

    await market_installer.install(stack, MU_REF, MU_PACKAGE, version="0.1.0")
    await market_installer.install(stack, NU_REF, NU_PACKAGE, version="0.1.0")
    await wait_tool_live(stack, MU_TOOL, dist_version="0.1.0")
    await wait_tool_live(stack, NU_TOOL, dist_version="0.1.0")

    # One resolution for both plugins: exactly one sharedlib, at the version both admit,
    # in the layer and never in the image.
    assert _layer_versions(stack, SHAREDLIB_PACKAGE) == [SHAREDLIB_V1_VERSION]
    assert layer_holds(stack, MU_PACKAGE)
    assert layer_holds(stack, NU_PACKAGE)
    assert not image_holds(SHAREDLIB_PACKAGE)

    await uninstall_and_assert_clean(stack, NU_REF, package=NU_PACKAGE, tool_name=NU_TOOL)
    # Mu still needs sharedlib, so the rebuilt layer keeps it.
    assert _layer_versions(stack, SHAREDLIB_PACKAGE) == [SHAREDLIB_V1_VERSION]
    await uninstall_and_assert_clean(stack, MU_REF, package=MU_PACKAGE, tool_name=MU_TOOL)
    assert not layer_holds(stack, SHAREDLIB_PACKAGE)
