"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

The core promise as one self-contained arc: a real wheel is pip-installed into the
serving venv, its tool imports and goes live over MCP and ``GET /api/tools``, the
attribution store records it with update availability, and uninstall reverts every
one of those — the venv included.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _market_support import (
    api_tool_names,
    distribution_absent,
    installed_refs,
    uninstall_and_assert_clean,
    wait_tool_live,
)

from tai42_e2e.marketplace import ALPHA_PACKAGE, ALPHA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

_TOOL = "e2e_market_probe"

# The alpha fixture's importable package; its ``.dist-info`` lands under the prefix
# when a prefix is configured (normalized name — dashes become underscores).
_ALPHA_DIST = "tai_e2e_market_alpha"


def _prefix_dist_present(stack: TaiStack) -> bool:
    """Whether the alpha distribution is installed UNDER the stack's plugin prefix
    (the persistent install root, a sibling of ``storage/``) — read from disk, so it
    reflects a real ``pip install --prefix`` and a real RECORD-based removal."""
    prefix = Path(stack.resources.storage_root).parent / "plugin-prefix"
    return any(list(site.glob(f"{_ALPHA_DIST}*")) for site in prefix.glob("lib/python*/site-packages"))


async def test_install_go_live_and_uninstall(
    marketplace_service: MarketplaceService, marketplace_stack: TaiStack
) -> None:
    stack = marketplace_stack

    # Precondition (negative first): the fixture tool is absent everywhere and the
    # distribution is not in the venv.
    assert _TOOL not in await api_tool_names(stack)
    async with stack.mcp() as mcp:
        assert _TOOL not in await mcp.tool_names()
    assert distribution_absent(ALPHA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})

    # The wheel is really installed, the module really imported, the app really
    # reloaded: the SAME process answers 0.1.0 from the installed distribution.
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    installed = await installed_refs(stack)
    assert set(installed) == {ALPHA_REF}
    row = installed[ALPHA_REF]
    assert row["version"] == "0.1.0"
    assert row["latest"] == "0.2.0"
    assert row["update_available"] is True

    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)


async def test_prefix_install_survives_a_serve_restart(marketplace_prefix_stack: TaiStack) -> None:
    stack = marketplace_prefix_stack

    # Precondition: the tool is absent, nothing is in the prefix, and the shared
    # editable venv does not carry the distribution.
    assert _TOOL not in await api_tool_names(stack)
    assert not _prefix_dist_present(stack)
    assert distribution_absent(ALPHA_PACKAGE)

    await stack.api().post("/api/marketplace/install", json={"ref": ALPHA_REF, "version": "0.1.0"})
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    # The install landed IN THE PREFIX, not the shared venv: the distribution is on
    # disk under the stack's plugin prefix, and the harness (venv-only path) still
    # cannot resolve it.
    assert _prefix_dist_present(stack)
    assert distribution_absent(ALPHA_PACKAGE)

    # A real serve-process restart — a fresh interpreter with empty import state.
    stack.restart("serve")

    # The plugin re-imports from the persistent prefix and serves again: boot put the
    # prefix back on sys.path BEFORE importing the manifest's modules, so the tool is
    # live over MCP and /api/tools after the restart.
    await wait_tool_live(stack, _TOOL, dist_version="0.1.0")

    # Uninstall removes the plugin's files from the prefix (a real RECORD-based delete)
    # and drops the row; the tool goes away and the prefix is clean again.
    await uninstall_and_assert_clean(stack, ALPHA_REF, package=ALPHA_PACKAGE, tool_name=_TOOL)
    assert not _prefix_dist_present(stack)
