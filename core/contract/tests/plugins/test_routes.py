"""Tests for the route-declaration models (``RouteDecl`` / ``RoutesDecl``), the item routes
rules, and the intra-spec route-collision detection."""

from __future__ import annotations

from typing import Any

import pytest


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spec_version": 1,
        "namespace": "tai42",
        "name": "toolbox",
        "display_name": "TAI Toolbox",
        "package": "tai42-toolbox",
        "version": "0.1.0",
        "description": "Generic tools and tool extensions.",
        "icon": "assets/toolbox.svg",
        "license": "Apache-2.0",
        "repository": "https://github.com/tai42ai/tai42/tree/main/plugins/toolbox",
        "contract": ">=0.1,<0.2",
        "categories": ["utilities"],
        "tags": ["uuid", "http"],
        "permissions": {"network": True},
        "provides": [
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "Generate a random UUID.",
                "tags": ["uuid"],
            }
        ],
    }
    base.update(overrides)
    return base


def _router_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "router",
        "name": "door",
        "module": "tai42_plugin.routers.door",
        "description": "A plugin router.",
        "routes": {
            "base": "relay/inbox",
            "paths": [{"path": "/events", "methods": ["POST"], "public": True}],
        },
    }
    item.update(overrides)
    return item


def _connector_provider(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "acme",
        "display_name": "Acme",
        "icon_url": "https://cdn.example.com/acme.png",
        "kind": "oauth",
        "origin": "system",
        "category": "productivity",
        "oauth": {"authorize": "https://acme.example.com/authorize", "token": "https://acme.example.com/token"},
        "client_id_env": "ACME_CLIENT_ID",
        "client_secret_env": "ACME_CLIENT_SECRET",
        "sub_services": {
            "mail": {
                "id": "mail",
                "display_name": "Mail",
                "scopes": ["mail.read"],
                "mcp_server": {"type": "http", "url": "https://acme.example.com/mcp"},
            }
        },
    }
    base.update(overrides)
    return base


def _connector_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "connector",
        "name": "acme",
        "provider": _connector_provider(),
        "description": "Acme connector.",
    }
    item.update(overrides)
    return item


def test_route_decl_accepts_literal_and_template_segments():
    from tai42_contract.plugins import RouteDecl

    route = RouteDecl(path="/events/{event_id}/ack", methods=["GET", "POST"], public=False)
    assert route.path == "/events/{event_id}/ack"
    assert route.methods == ["GET", "POST"]
    assert route.public is False


def test_route_decl_public_is_required():
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    with pytest.raises(ValidationError, match="public"):
        RouteDecl(path="/events", methods=["GET"])  # pyright: ignore[reportCallIssue]


def test_route_decl_path_must_be_slash_prefixed():
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    with pytest.raises(ValidationError, match="'/'-prefixed"):
        RouteDecl(path="events", methods=["GET"], public=True)


@pytest.mark.parametrize("bad", ["/", "/events/", "/events//ack", ""])
def test_route_decl_path_rejects_empty_and_trailing_segments(bad: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    with pytest.raises(ValidationError):
        RouteDecl(path=bad, methods=["GET"], public=True)


def test_route_decl_path_rejects_converter_suffix():
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    # A Starlette converter template is invalid — plugin declarations never
    # carry converters.
    with pytest.raises(ValidationError, match="converter suffix"):
        RouteDecl(path="/assets/{file:path}", methods=["GET"], public=True)


@pytest.mark.parametrize("bad", ["/events!", "/ev ent", "/{1bad}", "/{}"])
def test_route_decl_path_rejects_bad_segment_charset(bad: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    with pytest.raises(ValidationError):
        RouteDecl(path=bad, methods=["GET"], public=True)


@pytest.mark.parametrize("bad", ["/.", "/..", "/events/..", "/../secret", "/a/./b", "/a/../b"])
def test_route_decl_path_rejects_dot_traversal_segments(bad: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    # ``.`` and ``..`` are path-traversal tokens (they match the literal charset but
    # are never valid route segments) — rejected so a declaration can never carry one.
    with pytest.raises(ValidationError, match="path-traversal token"):
        RouteDecl(path=bad, methods=["GET"], public=True)


def test_route_decl_methods_non_empty_unique_and_uppercase():
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    with pytest.raises(ValidationError, match="at least one HTTP method"):
        RouteDecl(path="/events", methods=[], public=True)
    with pytest.raises(ValidationError, match="unique"):
        RouteDecl(path="/events", methods=["GET", "GET"], public=True)
    # The method vocabulary is the uppercase literal set: a lowercase verb is
    # rejected, never coerced.
    with pytest.raises(ValidationError):
        RouteDecl(path="/events", methods=["get"], public=True)  # pyright: ignore[reportArgumentType]


def test_route_decl_rejects_unknown_key():
    from pydantic import ValidationError

    from tai42_contract.plugins import RouteDecl

    with pytest.raises(ValidationError, match="weight"):
        RouteDecl(path="/events", methods=["GET"], public=True, weight=1)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("value", ["relay", "relay/inbox", "a1/b2-c3/d"])
def test_routes_decl_base_accepts_relative_segments(value: str):
    from tai42_contract.plugins import RoutesDecl

    decl = RoutesDecl(base=value, paths=[{"path": "/x", "methods": ["GET"], "public": True}])  # pyright: ignore[reportArgumentType]
    assert decl.base == value


@pytest.mark.parametrize("bad", ["/relay", "relay/", "relay//inbox", "Relay", "-relay", "relay/{id}", ""])
def test_routes_decl_base_rejects_bad_shapes(bad: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import RoutesDecl

    with pytest.raises(ValidationError):
        RoutesDecl(base=bad, paths=[{"path": "/x", "methods": ["GET"], "public": True}])  # pyright: ignore[reportArgumentType]


def test_routes_decl_paths_non_empty():
    from pydantic import ValidationError

    from tai42_contract.plugins import RoutesDecl

    with pytest.raises(ValidationError, match="at least one path"):
        RoutesDecl(base="relay", paths=[])


def test_routes_decl_rejects_unknown_key():
    from pydantic import ValidationError

    from tai42_contract.plugins import RoutesDecl

    with pytest.raises(ValidationError, match="mount"):
        RoutesDecl(
            base="relay",
            paths=[{"path": "/x", "methods": ["GET"], "public": True}],
            mount="/somewhere",  # pyright: ignore[reportCallIssue]
        )


def test_router_item_requires_routes():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    item = _router_item()
    del item["routes"]
    with pytest.raises(ValidationError, match="'router' requires 'routes'"):
        PluginItem(**item)


def test_channel_item_routes_are_optional():
    from tai42_contract.plugins import PluginItem, PluginItemKind

    without = PluginItem(kind=PluginItemKind.CHANNEL, name="web", module="a.b", description="A channel.")
    assert without.routes is None
    with_routes = PluginItem(
        kind=PluginItemKind.CHANNEL,
        name="web",
        module="a.b",
        description="A channel.",
        routes={"base": "channels/web", "paths": [{"path": "/messages", "methods": ["POST"], "public": True}]},  # pyright: ignore[reportArgumentType]
    )
    assert with_routes.routes is not None
    assert with_routes.routes.base == "channels/web"


@pytest.mark.parametrize("kind", ["tool", "agent", "extension", "middleware"])
def test_non_router_non_channel_items_forbid_routes(kind: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    with pytest.raises(ValidationError, match="must not set 'routes'"):
        PluginItem(
            kind=kind,  # pyright: ignore[reportArgumentType]
            name="x",
            module="a.b",
            description="d",
            routes={"base": "relay", "paths": [{"path": "/x", "methods": ["GET"], "public": True}]},  # pyright: ignore[reportArgumentType]
        )


def test_connector_item_forbids_routes():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    # A connector is a data item, not a mount owner: ``routes`` is forbidden the
    # same as for any non-router, non-channel kind.
    with pytest.raises(ValidationError, match="must not set 'routes'"):
        PluginItem(
            **_connector_item(routes={"base": "relay", "paths": [{"path": "/x", "methods": ["GET"], "public": True}]})
        )


def test_router_item_round_trips_routes():
    from tai42_contract.plugins import PluginItem, PluginItemKind

    item = PluginItem(**_router_item())
    assert item.kind is PluginItemKind.ROUTER
    assert item.routes is not None
    assert item.routes.paths[0].path == "/events"
    assert item.routes.paths[0].public is True


def test_spec_accepts_router_item_with_routes():
    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_spec_kwargs(provides=[_router_item()]))
    assert spec.provides[0].routes is not None


def test_spec_rejects_intra_spec_template_vs_concrete_collision():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # A concrete path instantiating a sibling item's template on a shared method
    # is a collision — one owner per (shape, method).
    provides = [
        _router_item(
            name="a", routes={"base": "relay", "paths": [{"path": "/ping", "methods": ["GET"], "public": True}]}
        ),
        _router_item(
            name="b",
            module="tai42_plugin.routers.b",
            routes={"base": "relay", "paths": [{"path": "/{anything}", "methods": ["GET"], "public": True}]},
        ),
    ]
    with pytest.raises(ValidationError, match="route collision"):
        PluginSpec(**_spec_kwargs(provides=provides))


def test_spec_rejects_intra_spec_template_vs_template_collision():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    provides = [
        _router_item(
            name="a", routes={"base": "relay", "paths": [{"path": "/{a}", "methods": ["POST"], "public": True}]}
        ),
        _router_item(
            name="b",
            module="tai42_plugin.routers.b",
            routes={"base": "relay", "paths": [{"path": "/{b}", "methods": ["POST"], "public": True}]},
        ),
    ]
    with pytest.raises(ValidationError, match="route collision"):
        PluginSpec(**_spec_kwargs(provides=provides))


def test_spec_accepts_same_shape_disjoint_methods():
    from tai42_contract.plugins import PluginSpec

    # Same resolved shape but non-overlapping methods in separate rows is legal.
    provides = [
        _router_item(
            name="a", routes={"base": "relay", "paths": [{"path": "/gate", "methods": ["GET"], "public": False}]}
        ),
        _router_item(
            name="b",
            module="tai42_plugin.routers.b",
            routes={"base": "relay", "paths": [{"path": "/gate", "methods": ["PUT"], "public": False}]},
        ),
    ]
    spec = PluginSpec(**_spec_kwargs(provides=provides))
    assert [item.name for item in spec.provides] == ["a", "b"]


def test_spec_accepts_disjoint_bases_and_segment_counts():
    from tai42_contract.plugins import PluginSpec

    # Different bases never overlap (literal prefix differs); different segment
    # counts never overlap either.
    provides = [
        _router_item(
            name="a", routes={"base": "relay/a", "paths": [{"path": "/x", "methods": ["GET"], "public": True}]}
        ),
        _router_item(
            name="b",
            module="tai42_plugin.routers.b",
            routes={"base": "relay/b", "paths": [{"path": "/x", "methods": ["GET"], "public": True}]},
        ),
        _router_item(
            name="c",
            module="tai42_plugin.routers.c",
            routes={"base": "relay", "paths": [{"path": "/x/y", "methods": ["GET"], "public": True}]},
        ),
    ]
    spec = PluginSpec(**_spec_kwargs(provides=provides))
    assert len(spec.provides) == 3


def test_spec_detects_collision_across_base_plus_path_shape():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # Two items whose base+path resolve to the same shape on a shared method
    # collide even though the raw ``path`` strings differ.
    provides = [
        _router_item(
            name="a", routes={"base": "relay/x", "paths": [{"path": "/y", "methods": ["GET"], "public": True}]}
        ),
        _router_item(
            name="b",
            module="tai42_plugin.routers.b",
            routes={"base": "relay", "paths": [{"path": "/x/y", "methods": ["GET"], "public": True}]},
        ),
    ]
    with pytest.raises(ValidationError, match="route collision"):
        PluginSpec(**_spec_kwargs(provides=provides))
