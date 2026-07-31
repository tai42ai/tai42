"""Q2 default-boot: the three ``default_routers`` modes end-to-end on real boots.

The skeleton's loader composes the served router set from ``Manifest.default_routers``:

* ``"all"`` — the full ``DEFAULT_API_ROUTERS`` set plus the SPA catch-all last (the
  full-Studio deployment). ``tests/routing/test_default_router_coverage.py`` boots
  this mode with an OMITTED ``routers_modules`` and asserts the whole surface is
  non-404, so it is not re-proven here.
* ``"api"`` — the default API routers but NO SPA catch-all: ``/api/*`` answers while
  ``/`` (and any client path) has no shell to serve. The headless JSON deployment.
* ``"none"`` — no defaults at all; ``routers_modules`` is authoritative. A router the
  operator did NOT list stays dark even though it is in the default set — the opt-out.

Non-404 means "the route is MOUNTED" (200/401/403/422 all qualify); only 404 is dark.
The discriminator across modes is ``/api/backup/sections``: a default router that the
minimal (``"none"``) stack never lists — mounted under ``"api"``, dark under ``"none"``.
"""

from __future__ import annotations

import httpx
import pytest

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# These stacks drive no backend worker — they are route-mounting only.
pytestmark = pytest.mark.backendless

# A default router that is present under "all"/"api" but is NOT in the minimal stack's
# explicit four-router list, so it is the clean opt-in/opt-out discriminator.
_DEFAULTED_UNLISTED = "/api/backup/sections"


async def _status(stack: TaiStack, method: str, path: str, json: dict | None = None) -> httpx.Response:
    """Issue one raw request against the app port, polling only past the boot-time
    reload gate (a ``503 reloading`` while a worker self-resyncs is not a route verdict)."""
    api = stack.api()

    async def settled() -> httpx.Response | None:
        resp = await api.request_raw(method, path, json=json)
        if resp.status_code == 503:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("reloading") is True:
                return None
        return resp

    return await wait_for_async(settled, deadline=15.0, message=f"{method} {path} never left the reload gate")


async def test_api_mode_serves_defaults_but_no_spa(api_router_stack: TaiStack) -> None:
    """``default_routers="api"``: the default API surface answers (a core route AND a
    default router the minimal stack omits are both non-404), but the SPA catch-all is
    absent — ``/`` and a client path have no shell (404), and an unknown ``/api`` path
    is still a genuine 404 (nothing is shadowing it)."""
    tools = await _status(api_router_stack, "GET", "/api/tools")
    assert tools.status_code != 404, f"api mode did not mount the core API: /api/tools -> {tools.status_code}"

    backup = await _status(api_router_stack, "GET", _DEFAULTED_UNLISTED)
    assert backup.status_code != 404, (
        f"api mode did not mount the full default set: {_DEFAULTED_UNLISTED} -> {backup.status_code}"
    )

    root = await _status(api_router_stack, "GET", "/")
    assert root.status_code == 404, f"api mode is headless, so / must not serve a SPA shell; got {root.status_code}"

    client_path = await _status(api_router_stack, "GET", "/dashboard")
    assert client_path.status_code == 404, (
        f"api mode has no SPA catch-all, so a client path must 404; got {client_path.status_code}"
    )

    unknown_api = await _status(api_router_stack, "GET", "/api/this-route-does-not-exist")
    assert unknown_api.status_code == 404, f"unknown /api path should 404 cleanly; got {unknown_api.status_code}"


async def test_none_mode_mounts_only_the_explicit_list(minimal_stack: TaiStack) -> None:
    """``default_routers="none"``: exactly the listed routers mount, nothing more. The
    minimal stack lists health/metrics/tools/config, so those answer non-404, while a
    router that IS in the default set but is NOT listed (backup, presets) stays dark —
    proving the opt-out fully disables the defaults — and ``/`` 404s (no catch-all)."""
    for path in ("/api/tools", "/api/config/mode"):
        resp = await _status(minimal_stack, "GET", path)
        assert resp.status_code != 404, f"none mode dropped a LISTED router: {path} -> {resp.status_code}"

    for path in (_DEFAULTED_UNLISTED, "/api/presets"):
        resp = await _status(minimal_stack, "GET", path)
        assert resp.status_code == 404, (
            f"none mode mounted an UNLISTED default router (opt-out failed): {path} -> {resp.status_code}"
        )

    root = await _status(minimal_stack, "GET", "/")
    assert root.status_code == 404, f"none mode listed no SPA catch-all, so / must 404; got {root.status_code}"
