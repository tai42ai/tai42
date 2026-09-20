"""The route-metadata registry — the single source of truth for the app's self-describing HTTP surface.

Every ``@tai42_app.http.custom_route(...)`` registration records a
:class:`RouteMetadata` entry here (see :mod:`tai42_skeleton.app.http`). Two
consumers read the registry through the shared enumeration primitive
:func:`load_api_routes`:

* the OpenAPI 3.1 emitter (:mod:`tai42_skeleton.cli.openapi`) and its coverage
  gate, which turns the registry into a spec and asserts every ``/api/*`` route
  self-describes; and
* the CLI↔route parity gate, which asserts every ``/api/*`` route has a terminal
  command.

The registry is populated purely by importing the router modules — no database,
Redis, or booted server — so the spec emits OFFLINE. The one exception is
:meth:`RouteRegistry.record_mounted`, which the serving app calls as it mounts each
MCP transport and the sub-MCP router: those paths are served by a mounted ASGI app
rather than a handler, so they exist only in a process that mounted them and are
marked ``mounted`` for the consumers that describe the handler surface alone.

Each route DECLARES its behavioral OpenAPI metadata (``reload_gated``,
``reads_body``, ``error_statuses``, ``success_status``) through
:class:`DeclaredRouteMetadata`: a route registered through the operations adapter
supplies it from its operation's metadata, and a native ``/api/*`` handler
passes it explicitly at its registration. A handler that declares nothing (a
route outside the ``/api/*`` spec surface, e.g. ``/health`` or ``/ready``)
records trivial defaults, since its behavioral metadata is never emitted.

``error_statuses`` are the statuses a route answers with the plain
``{"error": ...}`` envelope. The reload gate's ``503`` is not one of them — it
answers the constant-message reloading envelope with a ``Retry-After`` header — so
it is declared by ``reload_gated`` alone and the emitter owns its response. A route
declaring ``503`` therefore says it answers a PLAIN ``503`` too, and one declaring
both publishes a ``503`` admitting either body.

The per-method success CONTENT TYPE is derived from each handler's source: the
default JSON surface answers the ``{"data": ...}`` envelope, while a streaming,
CSV, HTML, or asset-serving route answers its own media type, which the emitter
documents faithfully.
"""

from __future__ import annotations

from tai42_skeleton.app.route_registry.metadata import (
    CORE_OWNER,
    CrossOwnerRouteCollisionError,
    DeclaredRouteMetadata,
    EpochRouteAuditError,
    RouteMetadata,
    RouteOwner,
)
from tai42_skeleton.app.route_registry.registry import RouteRegistry, route_registry
from tai42_skeleton.app.route_registry.route_action import (
    MOUNT_METHODS,
    Handler,
    RouteAction,
    derive_route_action,
    method_to_action,
)
from tai42_skeleton.app.route_registry.scan import (
    _import_all_router_modules,
    _SpecApp,
    _SpecLifecycle,
    load_all_routes,
    load_api_routes,
    route_action_violations,
)
from tai42_skeleton.app.route_shapes import Shape

__all__ = [
    "CORE_OWNER",
    "MOUNT_METHODS",
    "CrossOwnerRouteCollisionError",
    "DeclaredRouteMetadata",
    "EpochRouteAuditError",
    "Handler",
    "RouteAction",
    "RouteMetadata",
    "RouteOwner",
    "RouteRegistry",
    "Shape",
    "_SpecApp",
    "_SpecLifecycle",
    "_import_all_router_modules",
    "derive_route_action",
    "load_all_routes",
    "load_api_routes",
    "method_to_action",
    "route_action_violations",
    "route_registry",
]
