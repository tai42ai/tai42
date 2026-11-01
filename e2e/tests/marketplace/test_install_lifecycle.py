"""Skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The core promise as one self-contained arc: a real wheel is built into the stack's
plugin layer (never the image), its tool imports and goes live over MCP and
``GET /api/tools``, the attribution store records it with update availability, and
uninstall reverts every one of those — the layer included. A second arc proves the
layer outlives a serve-process restart.
"""

from __future__ import annotations

import pytest

from tai42_e2e.fixture_catalog import ALPHA_PACKAGE, ALPHA_REF
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.stack import TaiStack

from ._market_support import (
    api_tool_names,
    image_holds,
    installed_refs,
    layer_holds,
    plugin_absent,
    uninstall_and_assert_clean,
    wait_tool_live,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "mutable",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
        "setting:PIP_INDEX_URL",
        "setting:TAI_PLUGINS_DIR",
        "setting:fixture-plugin:alpha",
    ),
]

_TOOL = "e2e_market_probe"


async def test_install_go_live_and_uninstall(
    marketplace_service: MarketplaceService, marketplace_stack: TaiStack
) -> None:
    stack = marketplace_stack

    # Precondition (negative first): the fixture tool is absent everywhere and no
    # plugin environment holds the distribution.
    assert _TOOL not in await api_tool_names(stack)
    async with stack.mcp() as mcp:
        assert _TOOL not in await mcp.tool_names()
    assert plugin_absent(stack, ALPHA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})

    # The wheel is really installed, the module really imported, the app really
    # reloaded: the SAME process answers 0.1.0 from the installed distribution.
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")
    # It was built into the plugin layer, and the image never gained it.
    assert layer_holds(stack, ALPHA_PACKAGE)
    assert not image_holds(ALPHA_PACKAGE)

    installed = await installed_refs(stack)
    assert set(installed) == {ALPHA_REF}
    row = installed[ALPHA_REF]
    assert row["version"] == "0.1.0"
    assert row["latest"] == "0.2.0"
    assert row["update_available"] is True

    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)


@pytest.mark.needs("process", "files")
async def test_the_plugin_layer_survives_a_serve_restart(marketplace_stack: TaiStack) -> None:
    stack = marketplace_stack

    # Precondition: the tool is absent and neither the layer nor the image holds it.
    assert _TOOL not in await api_tool_names(stack)
    assert plugin_absent(stack, ALPHA_PACKAGE)
    assert not image_holds(ALPHA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    # The install landed IN THE LAYER, not the image: the distribution is on disk in
    # the layer's current build under the stack's plugins directory, and the harness
    # (image-only path) still cannot resolve it.
    assert layer_holds(stack, ALPHA_PACKAGE)
    assert not image_holds(ALPHA_PACKAGE)

    # A real serve-process restart — a fresh interpreter with empty import state.
    stack.restart("serve")

    # The plugin re-imports from the persistent layer and serves again: boot put the
    # layer's current build on sys.path BEFORE importing the manifest's modules, so the
    # tool is live over MCP and /api/tools after the restart.
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    # Uninstall rebuilds the layer without the plugin and drops the row; the tool goes
    # away and the layer no longer holds it.
    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)
    assert not layer_holds(stack, ALPHA_PACKAGE)
