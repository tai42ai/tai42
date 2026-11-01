"""An update rebuilds the plugin layer whole. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Mu 0.1.0 depends on the dependency-only ``tai-e2e-market-sharedlib``; mu 0.2.0 drops
it. The update builds a fresh layer from the installed plugins at their new pins, so
the dependency the old version needed is gone from the layer — nothing of the old
version's closure lingers beside the new one.
"""

from __future__ import annotations

import pytest

from tai42_e2e.fixture_catalog import MU_PACKAGE, MU_REF, MU_TOOL, SHAREDLIB_PACKAGE
from tai42_e2e.stack import TaiStack

from ._market_support import (
    MarketInstaller,
    image_holds,
    installed_refs,
    layer_holds,
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
    ),
]


async def test_an_update_that_drops_a_dependency_removes_it_from_the_layer(
    mu_nu_catalog: None, marketplace_stack: TaiStack, market_installer: MarketInstaller
) -> None:
    stack = marketplace_stack

    await market_installer.install(stack, MU_REF, MU_PACKAGE, version="0.1.0")
    await wait_tool_live(stack, MU_TOOL, dist_version="0.1.0")
    assert layer_holds(stack, SHAREDLIB_PACKAGE)

    await stack.api().post("/api/marketplace/update", json={"ref": MU_REF, "version": "0.2.0"})

    # The SAME tool answers from mu 0.2.0, the store follows, and the rebuilt layer no
    # longer holds the dependency only mu 0.1.0 needed — nor did the image ever gain it.
    await wait_tool_live(stack, MU_TOOL, dist_version="0.2.0")
    assert (await installed_refs(stack))[MU_REF]["version"] == "0.2.0"
    assert not layer_holds(stack, SHAREDLIB_PACKAGE)
    assert not image_holds(SHAREDLIB_PACKAGE)

    await uninstall_and_assert_clean(stack, MU_REF, package=MU_PACKAGE, tool_name=MU_TOOL)
