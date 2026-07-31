"""C7 — skeleton ↔ tai42-marketplace registry over HTTP. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Two failure surfaces, both on the gamma listing whose catalog state no other spec
depends on: a killed version refuses installation loudly (409, nothing installed),
and a registry outage makes the proxying surface fail loudly (502, never a skip)
and recover cleanly.
"""

from __future__ import annotations

import pytest
from _market_support import api_tool_names, distribution_absent, installed_refs

from tai42_e2e.marketplace import GAMMA_PACKAGE, GAMMA_REF, MarketplaceService
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

_TOOL = "e2e_market_gamma_probe"


async def test_killed_version_refuses_install(
    marketplace_service: MarketplaceService, marketplace_stack: TaiStack
) -> None:
    mp = marketplace_service
    stack = marketplace_stack

    await mp.api.post(
        "/api/v1/admin/kill-version",
        json={"ref": GAMMA_REF, "version": "0.1.0"},
        headers=mp.admin_headers,
    )

    # Pin the killed version explicitly: gamma has no other published version, so an
    # unpinned install would resolve-latest to a 404, not the killed-version refusal.
    body = await stack.api().post(
        "/api/marketplace/install",
        json={"ref": GAMMA_REF, "version": "0.1.0"},
        expect=409,
    )
    assert "0.1.0" in body["error"]
    assert "killed" in body["error"]

    # Nothing was installed.
    assert GAMMA_REF not in await installed_refs(stack)
    assert _TOOL not in await api_tool_names(stack)
    assert distribution_absent(GAMMA_PACKAGE)


async def test_registry_outage_fails_loudly_then_recovers(
    marketplace_service: MarketplaceService, marketplace_stack: TaiStack
) -> None:
    mp = marketplace_service
    stack = marketplace_stack

    mp.stop()
    try:
        # The install proxy fails loudly (502), never a silent success or a skip.
        install_err = await stack.api().post("/api/marketplace/install", json={"ref": GAMMA_REF}, expect=502)
        assert "unreachable" in install_err["error"]

        # The read proxy fails loudly too.
        search_err = await stack.api().get("/api/marketplace/search?q=probe", expect=502)
        assert "unreachable" in search_err["error"]
    finally:
        mp.start()

    # Recovery is part of the spec: the read proxy works again once the registry is back.
    recovered = await stack.api().get("/api/marketplace/search?q=probe")
    assert isinstance(recovered["items"], list)
