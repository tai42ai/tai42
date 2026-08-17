"""The installer's declared-route pre-flight and the preview door.

The route pre-flight resolves each item's mount base (default, stored, or operator
override), collision-checks every declared route against an injected live-registry
ownership set, enforces the reserved-prefix rule on public rows, and gates public
routes behind explicit acceptance — an install requires acceptance of every public
row, an update only of rows not already approved. The preview reports the same
picture with no side effects. Every seam is a recording fake (see
``test_installer.Harness``); the ownership set and reserved prefixes are injected so
each case is deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.app.route_shapes import parse_shape
from tai42_skeleton.marketplace.errors import (
    PublicRoutesNotAcceptedError,
    ReservedRoutePrefixError,
    RouteCollisionError,
    RouteMountError,
)
from tai42_skeleton.marketplace.routes import OwnedRoute
from tests.marketplace._specs import make_resolved, make_spec, router_item

from .test_installer import Harness, installer_module


def _owned(path: str, methods: list[str], *, label: str = "core", ref: str | None = None) -> OwnedRoute:
    """One already-owned registry route for the injected ownership set."""
    return OwnedRoute(shape=parse_shape(path), methods=frozenset(methods), owner_label=label, owner_ref=ref, path=path)


def _reserved() -> list[str]:
    """The injected reserved never-public prefixes."""
    return ["/api/auth"]


@pytest.fixture(autouse=True)
def _stamp_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The attribution write stamps the running core versions; pin them so a full
    # install through the harness needs no live distribution metadata.
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")


def _router_spec(*, base: str = "relay", paths: list[dict[str, Any]] | None = None):
    return make_spec(
        namespace="acme", name="relay", package="acme-relay", provides=[router_item(base=base, paths=paths)]
    )


# -- collision matrix --------------------------------------------------------


async def test_install_collides_with_a_core_route() -> None:
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=lambda: [_owned("/api/relay/status", ["GET"])], reserved_prefixes=_reserved)

    with pytest.raises(RouteCollisionError) as exc:
        await inst.install("acme/relay")

    (collision,) = exc.value.collisions
    assert collision["full_path"] == "/api/relay/status"
    assert collision["conflict_owner"] == "core"
    assert collision["conflict_path"] == "/api/relay/status"
    # The clash is caught BEFORE pip — nothing installed.
    assert h.pip.calls == []
    assert h.store.record_calls == []


async def test_install_collides_with_another_installed_plugin() -> None:
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    owned = [_owned("/api/relay/status", ["GET"], label="plugin:acme/other", ref="acme/other")]
    inst = h.installer(owned_routes=lambda: owned, reserved_prefixes=_reserved)

    with pytest.raises(RouteCollisionError) as exc:
        await inst.install("acme/relay")

    assert exc.value.collisions[0]["conflict_owner"] == "plugin:acme/other"


async def test_install_template_overlaps_a_concrete_owned_route() -> None:
    # A declared template segment overlaps a concrete owned path on the same method.
    h = Harness()
    spec = _router_spec(paths=[{"path": "/{anything}", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=lambda: [_owned("/api/relay/ping", ["GET"])], reserved_prefixes=_reserved)

    with pytest.raises(RouteCollisionError):
        await inst.install("acme/relay")


async def test_install_no_collision_when_methods_are_disjoint() -> None:
    # Same shape, disjoint methods is not a collision — the authed POST installs
    # cleanly beside a core GET on the same path (no public rows to accept).
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["POST"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=lambda: [_owned("/api/relay/status", ["GET"])], reserved_prefixes=_reserved)

    result = await inst.install("acme/relay")
    assert result["routes"] == [
        {"item": "relay", "full_path": "/api/relay/status", "methods": ["POST"], "public": False}
    ]


# -- reserved prefix ---------------------------------------------------------


async def test_install_public_route_under_reserved_prefix_is_refused() -> None:
    h = Harness()
    spec = _router_spec(base="auth", paths=[{"path": "/login", "methods": ["POST"], "public": True}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    with pytest.raises(ReservedRoutePrefixError) as exc:
        await inst.install("acme/relay", accept_public_routes=True)

    assert exc.value.offenders == ["/api/auth/login"]
    assert h.pip.calls == []


async def test_install_authed_route_under_reserved_prefix_is_allowed() -> None:
    # The reserved prefix bars only PUBLIC rows; an authed route mounts there fine.
    h = Harness()
    spec = _router_spec(base="auth", paths=[{"path": "/users", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    result = await inst.install("acme/relay")
    assert result["routes"][0]["full_path"] == "/api/auth/users"


def test_reserved_public_offenders_canonicalizes_the_full_path() -> None:
    # Defense-in-depth: a public route whose RAW resolved path is
    # /api/relay/../auth/keys falls under the reserved /api/auth ONLY after
    # dot-resolution. The audit canonicalizes before comparing, so "reserved always
    # wins" holds — matching the boot mount-map audit and the runtime verifier. A raw
    # startswith comparison would miss it.
    from tai42_skeleton.marketplace.routes import ResolvedRoute, reserved_public_offenders

    route = ResolvedRoute(
        item="web",
        kind="router",
        base="relay/..",
        default_base="relay",
        path="/keys",
        full_path="/api/relay/../auth/keys",
        methods=("GET",),
        public=True,
    )
    assert reserved_public_offenders([route], ["/api/auth"]) == [route]
    # A public route whose canonical path is offside is left alone (no over-reject).
    assert reserved_public_offenders([route], ["/api/nope"]) == []


# -- public acceptance -------------------------------------------------------


async def test_install_public_route_needs_acceptance() -> None:
    h = Harness()
    spec = _router_spec(
        paths=[
            {"path": "/status", "methods": ["GET"], "public": True},
            {"path": "/events", "methods": ["POST"], "public": False},
        ]
    )
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    with pytest.raises(PublicRoutesNotAcceptedError) as exc:
        await inst.install("acme/relay")

    # Only the public row is listed; the authed one never needs acceptance.
    assert exc.value.public_routes == [{"item": "relay", "full_path": "/api/relay/status", "methods": ["GET"]}]
    assert h.pip.calls == []


async def test_install_with_acceptance_persists_mounts_and_reports_routes() -> None:
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": True}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    result = await inst.install("acme/relay", route_mounts={"relay": "channels/relay-2"}, accept_public_routes=True)

    # The resolved per-item base is persisted for the boot mount-map.
    assert h.store.record_calls[-1][-1] == {"relay": "channels/relay-2"}
    row = h.store.rows["acme/relay"]
    assert row.route_mounts == {"relay": "channels/relay-2"}
    # The receipt lists the mounted route at the remapped base.
    assert result["routes"] == [
        {"item": "relay", "full_path": "/api/channels/relay-2/status", "methods": ["GET"], "public": True}
    ]


async def test_install_unknown_mount_item_is_a_bad_request() -> None:
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    with pytest.raises(RouteMountError):
        await inst.install("acme/relay", route_mounts={"nope": "somewhere"})
    assert h.pip.calls == []


async def test_install_bad_mount_base_is_a_bad_request() -> None:
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    with pytest.raises(RouteMountError):
        await inst.install("acme/relay", route_mounts={"relay": "/leading-slash"})


# -- update: public delta + surviving-item base ------------------------------


def _preload_relay(
    h: Harness, *, base: str = "relay", paths: list[dict[str, Any]], route_mounts: dict[str, str]
) -> None:
    old = _router_spec(base=base, paths=paths)
    h.store.preload(old, version="1.0.0")
    h.store.rows[old.ref] = h.store.rows[old.ref].model_copy(update={"route_mounts": route_mounts})


async def test_update_only_new_public_rows_need_acceptance() -> None:
    h = Harness()
    _preload_relay(
        h,
        paths=[{"path": "/status", "methods": ["GET"], "public": True}],
        route_mounts={"relay": "relay"},
    )
    new = _router_spec(
        paths=[
            {"path": "/status", "methods": ["GET"], "public": True},
            {"path": "/live", "methods": ["GET"], "public": True},
        ]
    )
    h.registry.resolved = make_resolved(new, version="2.0.0")
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    with pytest.raises(PublicRoutesNotAcceptedError) as exc:
        await inst.update("acme/relay")

    # Only the NEW public row is listed; the already-approved one is not re-asked.
    assert exc.value.public_routes == [{"item": "relay", "full_path": "/api/relay/live", "methods": ["GET"]}]


async def test_update_keeps_a_surviving_items_stored_base() -> None:
    h = Harness()
    _preload_relay(
        h,
        paths=[{"path": "/status", "methods": ["GET"], "public": False}],
        route_mounts={"relay": "channels/relay-2"},
    )
    new = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(new, version="2.0.0")
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    result = await inst.update("acme/relay")
    # No override given, so the surviving item keeps its stored (remapped) base.
    assert h.store.rows["acme/relay"].route_mounts == {"relay": "channels/relay-2"}
    assert result["routes"][0]["full_path"] == "/api/channels/relay-2/status"


async def test_update_excludes_own_installed_routes_from_collision() -> None:
    # The live registry still owns the OLD version's routes under this plugin's ref;
    # a surviving route must not self-collide.
    h = Harness()
    _preload_relay(
        h,
        paths=[{"path": "/status", "methods": ["GET"], "public": False}],
        route_mounts={"relay": "relay"},
    )
    new = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(new, version="2.0.0")
    owned = [_owned("/api/relay/status", ["GET"], label="plugin:acme/relay", ref="acme/relay")]
    inst = h.installer(owned_routes=lambda: owned, reserved_prefixes=_reserved)

    result = await inst.update("acme/relay")
    assert result["version"] == "2.0.0"


# -- preview -----------------------------------------------------------------


async def test_preview_fresh_install_reports_routes_and_public_rows() -> None:
    h = Harness()
    spec = _router_spec(
        paths=[
            {"path": "/status", "methods": ["GET"], "public": True},
            {"path": "/events", "methods": ["POST"], "public": False},
        ]
    )
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    out = await inst.preview("acme/relay", route_mounts={"relay": "channels/relay-2"})

    assert out["ref"] == "acme/relay"
    (item,) = out["items"]
    assert item["base"] == "channels/relay-2"
    assert item["default_base"] == "relay"
    assert {r["full_path"] for r in item["routes"]} == {
        "/api/channels/relay-2/status",
        "/api/channels/relay-2/events",
    }
    assert out["collisions"] == []
    assert out["public_routes"] == [{"item": "relay", "full_path": "/api/channels/relay-2/status", "methods": ["GET"]}]
    # A fresh install: every public row is new.
    assert out["new_public_routes"] == out["public_routes"]
    assert out["requires_public_acceptance"] is True
    # Preview makes NO state change.
    assert h.pip.calls == []
    assert h.store.record_calls == []


async def test_preview_reports_collisions_without_raising() -> None:
    h = Harness()
    spec = _router_spec(paths=[{"path": "/status", "methods": ["GET"], "public": False}])
    h.registry.resolved = make_resolved(spec)
    inst = h.installer(owned_routes=lambda: [_owned("/api/relay/status", ["GET"])], reserved_prefixes=_reserved)

    out = await inst.preview("acme/relay")
    assert out["collisions"][0]["conflict_owner"] == "core"


async def test_preview_update_narrows_new_public_rows() -> None:
    h = Harness()
    _preload_relay(
        h,
        paths=[{"path": "/status", "methods": ["GET"], "public": True}],
        route_mounts={"relay": "relay"},
    )
    new = _router_spec(
        paths=[
            {"path": "/status", "methods": ["GET"], "public": True},
            {"path": "/live", "methods": ["GET"], "public": True},
        ]
    )
    h.registry.resolved = make_resolved(new, version="2.0.0")
    inst = h.installer(owned_routes=list, reserved_prefixes=_reserved)

    out = await inst.preview("acme/relay")
    # Both are public, but only the new row is not already approved.
    assert len(out["public_routes"]) == 2
    assert out["new_public_routes"] == [{"item": "relay", "full_path": "/api/relay/live", "methods": ["GET"]}]
    assert out["requires_public_acceptance"] is True
