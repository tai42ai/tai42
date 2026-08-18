"""Operator-mountable plugin routes, end to end. Opt-in: collects only with
``TAI_E2E_MARKETPLACE=1``.

Four legs over the real stack, each driving the ``/api/marketplace/*`` install
surface (preview + install + update) and reading the served routes back:

1. REMAP + PERSISTENCE — install epsilon with a remapped mount base: the routes
   serve at the remapped path and 404 at the default path, the install receipt and
   persisted manifest show the mount, and a serve RESTART re-boots on the persisted
   store and STILL serves at the remapped path.
2. COLLISION — install epsilon, then theta (whose template ``/{slug}`` overlaps
   epsilon's concrete GET routes at the shared base): the second install is a
   ``409 ROUTE_COLLISION`` (preview lists the clash too); retrying with a remapped
   base installs, and both plugins serve.
3. PUBLIC ACCEPTANCE + the per-method pin (access control ON) — installing
   without acceptance is a ``400 PUBLIC_ROUTES_NOT_ACCEPTED`` listing the public
   rows (preview flags them); with acceptance the declared-public route answers
   UNAUTHENTICATED while its sibling AUTHED route/method still rejects an anonymous
   caller.
4. UPDATE adding a public route — bumping epsilon to the version whose spec declares
   an extra public route: the update without acceptance is a ``400`` listing ONLY the
   NEW row (the already-approved public route is not re-listed); with acceptance the
   new route goes live.
"""

from __future__ import annotations

import httpx
import pytest
from _market_support import (
    distribution_absent,
    installed_refs,
    persisted_manifest,
    skip_unless_registry_supports_declared_routes,
)

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.marketplace import (
    EPSILON_PACKAGE,
    EPSILON_REF,
    EPSILON_V2_VERSION,
    THETA_PACKAGE,
    THETA_REF,
    FixtureArtifacts,
    MarketplaceService,
    seed_epsilon_listing,
    seed_epsilon_v2_listing,
    seed_theta_listing,
)
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.backendless

_ROUTER_MODULE = "tai_e2e_market_epsilon.router"
_EPSILON_ITEM = "e2e_epsilon_router"
_THETA_ITEM = "e2e_theta_router"

_DEFAULT_PING = "/api/e2e-epsilon/ping"
_DEFAULT_OPEN = "/api/e2e-epsilon/open"
_DEFAULT_PROBE = "/api/e2e-epsilon/probe"


@pytest.fixture(scope="module")
def route_fixtures_seeded(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
) -> None:
    """Publish epsilon ``0.1.0`` + ``0.2.0`` and theta ``0.1.0`` into this module's
    registry through the real admin-seed + ingest pipeline. Kept out of the shared
    browse catalog, so this spec seeds exactly the listings it installs.

    Skips this module's legs when the pinned registry cannot accept declared routes
    (the fixtures declare ``routes``); ``marketplace_service`` has already built the
    registry venv the gate reads."""
    import asyncio

    skip_unless_registry_supports_declared_routes()

    async def seed() -> None:
        await seed_epsilon_listing(marketplace_service, package_index, fixture_artifacts)
        await seed_epsilon_v2_listing(marketplace_service, package_index, fixture_artifacts)
        await seed_theta_listing(marketplace_service, package_index, fixture_artifacts)

    asyncio.run(seed())


# ---- request helpers ----------------------------------------------------


async def _settled(client: ApiClient, method: str, path: str, *, deadline: float = 25.0) -> httpx.Response:
    """One request settled past the boot/reload gate — a ``503`` while a worker
    reloads is not a route verdict, so it is polled past; a genuine status returns."""

    async def probe() -> httpx.Response | None:
        resp = await client.request_raw(method, path)
        return None if resp.status_code == 503 else resp

    return await wait_for_async(probe, deadline=deadline, message=f"{method} {path} never left the reload gate")


async def _wait_status(client: ApiClient, path: str, expected: int, *, deadline: float = 25.0) -> None:
    """Poll ``GET path`` until it answers ``expected`` — the hot load/unload lands the
    instant the reload swap completes, but a bounded wait absorbs the reload tail."""

    async def reached() -> bool:
        resp = await client.request_raw("GET", path)
        return resp.status_code == expected

    await wait_for_async(reached, deadline=deadline, message=f"GET {path} never reached status {expected}")


async def _install(
    stack: TaiStack,
    ref: str,
    *,
    version: str,
    mounts: dict[str, str] | None = None,
    accept: bool = False,
) -> dict:
    body: dict[str, object] = {"ref": ref, "version": version, "accept_public_routes": accept}
    if mounts is not None:
        body["route_mounts"] = mounts
    return await stack.api().post("/api/marketplace/install", json=body, timeout=240.0)


async def _uninstall_clean(stack: TaiStack, ref: str, package: str) -> None:
    """Uninstall and prove the clean tail: the attribution row is gone and the
    distribution no longer resolves in the shared venv (the venv guard would
    otherwise raise for the whole area)."""
    await stack.api().post("/api/marketplace/uninstall", json={"ref": ref}, timeout=240.0)

    async def gone() -> bool:
        if ref in await installed_refs(stack):
            return False
        return distribution_absent(package)

    await wait_for_async(gone, deadline=30.0, message=f"{ref} never fully uninstalled (row or {package!r} lingered)")


# ---- 1. remap + persistence across restart ------------------------------


async def test_remap_serves_at_new_base_and_survives_restart(
    route_fixtures_seeded: None,
    router_merge_stack: TaiStack,
) -> None:
    stack = router_merge_stack
    remapped_base = "e2e-epsilon-moved"
    remapped_ping = "/api/e2e-epsilon-moved/ping"
    remapped_open = "/api/e2e-epsilon-moved/open"

    assert distribution_absent(EPSILON_PACKAGE)
    assert (await _settled(stack.api(), "GET", remapped_ping)).status_code == 404

    result = await _install(stack, EPSILON_REF, version="0.1.0", mounts={_EPSILON_ITEM: remapped_base}, accept=True)
    try:
        # The install receipt shows the remapped mount, nothing at the default base.
        mounted = {row["full_path"] for row in result["routes"]}
        assert mounted == {remapped_ping, remapped_open}, (
            f"receipt did not carry the remapped routes: {result['routes']}"
        )
        assert _DEFAULT_PING not in mounted

        # The persisted manifest shows the router module mounted.
        routers = persisted_manifest(stack).get("routers_modules") or []
        assert _ROUTER_MODULE in routers, f"router module not persisted: {routers}"

        # The persisted store row carries the remapped mount base directly (the row
        # the mount map reads on the next boot).
        row = (await installed_refs(stack))[EPSILON_REF]
        assert row["route_mounts"] == {_EPSILON_ITEM: remapped_base}, (
            f"persisted route_mounts did not carry the remap: {row.get('route_mounts')}"
        )

        # Serves at the remapped path, 404 at the default path.
        await _wait_status(stack.api(), remapped_ping, 200)
        await _wait_status(stack.api(), remapped_open, 200)
        assert (await _settled(stack.api(), "GET", _DEFAULT_PING)).status_code == 404
        assert (await _settled(stack.api(), "GET", _DEFAULT_OPEN)).status_code == 404

        # RESTART: re-boots on the persisted store (the mount map reads the row's
        # route_mounts) and STILL serves at the remapped path, still dark at the default.
        stack.restart("serve")
        await _wait_status(stack.api(), remapped_ping, 200)
        assert (await _settled(stack.api(), "GET", _DEFAULT_PING)).status_code == 404

        # The persisted mount survives the restart in the store row too.
        row = (await installed_refs(stack))[EPSILON_REF]
        assert row["route_mounts"] == {_EPSILON_ITEM: remapped_base}, (
            f"route_mounts did not survive the restart: {row.get('route_mounts')}"
        )
    finally:
        await _uninstall_clean(stack, EPSILON_REF, EPSILON_PACKAGE)


# ---- 2. collision, then remap remedy ------------------------------------


async def test_route_collision_then_remap_installs_both(
    route_fixtures_seeded: None,
    router_merge_stack: TaiStack,
) -> None:
    stack = router_merge_stack
    theta_base = "e2e-theta"
    theta_path = "/api/e2e-theta/anything"

    await _install(stack, EPSILON_REF, version="0.1.0", accept=True)
    try:
        await _wait_status(stack.api(), _DEFAULT_PING, 200)

        # Preview theta at its default base: the collision is listed before any install.
        preview = await stack.api().post(
            "/api/marketplace/install/preview", json={"ref": THETA_REF, "version": "0.1.0"}
        )
        assert preview["collisions"], f"preview did not surface the collision: {preview}"

        # Installing theta at the default base is a 409 ROUTE_COLLISION.
        resp = await stack.api().request_raw(
            "POST",
            "/api/marketplace/install",
            json={"ref": THETA_REF, "version": "0.1.0", "accept_public_routes": True},
            timeout=240.0,
        )
        assert resp.status_code == 409, f"expected a collision 409, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["code"] == "ROUTE_COLLISION"
        # The error states the remap remedy, so the operator sees the way out.
        assert "remap" in body["error"].lower(), f"collision body did not state the remap remedy: {body.get('error')!r}"
        clash = body["collisions"]
        assert clash, f"collision body carried no rows: {body}"
        assert clash[0]["item"] == _THETA_ITEM
        assert clash[0]["conflict_owner"] == f"plugin:{EPSILON_REF}", clash[0]

        # Preview with the remapped base clears the collision, then the install lands.
        remapped_preview = await stack.api().post(
            "/api/marketplace/install/preview",
            json={"ref": THETA_REF, "version": "0.1.0", "route_mounts": {_THETA_ITEM: theta_base}},
        )
        assert remapped_preview["collisions"] == [], remapped_preview

        await _install(stack, THETA_REF, version="0.1.0", mounts={_THETA_ITEM: theta_base}, accept=True)
        try:
            # Both plugins serve: epsilon at its base, theta at the remapped base.
            await _wait_status(stack.api(), theta_path, 200)
            ping = await stack.api().request_raw("GET", _DEFAULT_PING)
            assert ping.status_code == 200, ping.text
            assert ping.json()["data"]["epsilon"] == "pong", ping.text
            theta = await stack.api().request_raw("GET", theta_path)
            assert theta.status_code == 200, theta.text
            assert theta.json()["data"]["theta"] == "anything", theta.text
        finally:
            await _uninstall_clean(stack, THETA_REF, THETA_PACKAGE)
    finally:
        await _uninstall_clean(stack, EPSILON_REF, EPSILON_PACKAGE)


# ---- 3. public acceptance + the per-method pin ----------------------


async def test_public_acceptance_and_per_method_pin(
    route_fixtures_seeded: None,
    marketplace_authz_stack: TaiStack,
) -> None:
    stack = marketplace_authz_stack
    authed = stack.api()  # carries the seeded root token — the fenced install door needs it
    anon = ApiClient(f"http://{stack.host}:{stack.port_a}")  # no token — the anonymous caller

    # Preview flags the public route requiring acceptance.
    preview = await authed.post("/api/marketplace/install/preview", json={"ref": EPSILON_REF, "version": "0.1.0"})
    assert preview["requires_public_acceptance"] is True
    assert _DEFAULT_OPEN in {row["full_path"] for row in preview["public_routes"]}, preview

    # Install WITHOUT acceptance: a 400 listing the public rows.
    resp = await authed.request_raw(
        "POST", "/api/marketplace/install", json={"ref": EPSILON_REF, "version": "0.1.0"}, timeout=240.0
    )
    assert resp.status_code == 400, f"expected a public-acceptance 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == "PUBLIC_ROUTES_NOT_ACCEPTED"
    assert _DEFAULT_OPEN in {row["full_path"] for row in body["public_routes"]}, body

    # Install WITH acceptance.
    await _install(stack, EPSILON_REF, version="0.1.0", accept=True)
    try:
        # The declared-public route answers UNAUTHENTICATED.
        await _wait_status(anon, _DEFAULT_OPEN, 200)
        open_resp = await anon.request_raw("GET", _DEFAULT_OPEN)
        assert open_resp.status_code == 200, open_resp.text
        assert open_resp.json()["data"]["epsilon"] == "open", open_resp.text

        # Its sibling AUTHED route still rejects an anonymous caller — the
        # per-method pin: public is granted by declaration per (path, method), never a
        # spillover onto the sibling route.
        ping = await _settled(anon, "GET", _DEFAULT_PING)
        assert ping.status_code in (401, 403), f"authed sibling did not reject anonymous: {ping.status_code}"

        # The pin is per-METHOD on a SHARED path too: GET /open is public (answered
        # above) while the authed POST /open sibling METHOD on the SAME path rejects
        # the anonymous caller — the public grant never spills onto the sibling method.
        open_post = await _settled(anon, "POST", _DEFAULT_OPEN)
        assert open_post.status_code in (401, 403), (
            f"authed sibling method on the shared path did not reject anonymous: {open_post.status_code}"
        )
    finally:
        await _uninstall_clean(stack, EPSILON_REF, EPSILON_PACKAGE)


# ---- 4. update adding a new public route --------------------------------


async def test_update_adding_public_route_requires_acceptance_of_only_the_new_row(
    route_fixtures_seeded: None,
    router_merge_stack: TaiStack,
) -> None:
    stack = router_merge_stack

    # Install 0.1.0 accepting its one public route, so /open is already approved.
    await _install(stack, EPSILON_REF, version="0.1.0", accept=True)
    try:
        await _wait_status(stack.api(), _DEFAULT_OPEN, 200)

        # Preview the update: only the NEW public row needs acceptance (the already-approved
        # /open is not re-listed).
        preview = await stack.api().post(
            "/api/marketplace/install/preview", json={"ref": EPSILON_REF, "version": EPSILON_V2_VERSION}
        )
        assert preview["requires_public_acceptance"] is True
        assert {row["full_path"] for row in preview["new_public_routes"]} == {_DEFAULT_PROBE}, preview

        # Update WITHOUT acceptance: a 400 listing ONLY the new row.
        resp = await stack.api().request_raw(
            "POST",
            "/api/marketplace/update",
            json={"ref": EPSILON_REF, "version": EPSILON_V2_VERSION},
            timeout=240.0,
        )
        assert resp.status_code == 400, f"expected a public-acceptance 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["code"] == "PUBLIC_ROUTES_NOT_ACCEPTED"
        assert {row["full_path"] for row in body["public_routes"]} == {_DEFAULT_PROBE}, body

        # Update WITH acceptance: the new public route goes live.
        await stack.api().post(
            "/api/marketplace/update",
            json={"ref": EPSILON_REF, "version": EPSILON_V2_VERSION, "accept_public_routes": True},
            timeout=240.0,
        )
        await _wait_status(stack.api(), _DEFAULT_PROBE, 200)
        probe = await stack.api().request_raw("GET", _DEFAULT_PROBE)
        assert probe.status_code == 200, probe.text
        assert probe.json()["data"]["epsilon"] == "probe", probe.text
    finally:
        await _uninstall_clean(stack, EPSILON_REF, EPSILON_PACKAGE)
