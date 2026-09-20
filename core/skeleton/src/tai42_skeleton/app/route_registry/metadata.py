"""Route metadata + owner value types and the route-registry errors.

The self-describing shape each registered route carries and the loud failures it raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from tai42_contract.app import DeclaredRouteMetadata

from tai42_skeleton.app.route_registry.route_action import RouteAction
from tai42_skeleton.app.route_shapes import Shape

__all__ = [
    "CORE_OWNER",
    "CrossOwnerRouteCollisionError",
    "DeclaredRouteMetadata",
    "EpochRouteAuditError",
    "RouteMetadata",
    "RouteOwner",
]


class CrossOwnerRouteCollisionError(RuntimeError):
    """A route registration whose shape+methods collide with a DIFFERENT owner's registered route.

    Raised to kill silent registry shadowing by construction — one owner per route, enforced at
    registration.
    """


class EpochRouteAuditError(RuntimeError):
    """An epoch rebuild dropped EVERY HTTP route of a plugin still declared in the manifest.

    Raised before the atomic commit so the build discards the staged generation and the OLD
    epoch keeps serving — a loud reload failure instead of a silent route unmount.
    """


@dataclass(frozen=True)
class RouteOwner:
    """Who registered a route: ``core`` for a native/operator route, ``plugin`` for a plugin route.

    For a plugin route ``owner_ref`` is the ``namespace/name`` listing and ``item_name`` the
    provided item. The identity the cross-owner collision check compares — one owner per route
    shape.
    """

    kind: Literal["core", "plugin"] = "core"
    owner_ref: str | None = None
    item_name: str | None = None


CORE_OWNER = RouteOwner(kind="core")


@dataclass(frozen=True)
class RouteMetadata:
    """One self-describing route: its wire shape plus the OpenAPI metadata downstream gates consume.

    Consumed by the emitter and the coverage/parity gates.
    """

    path: str
    methods: tuple[str, ...]
    name: str
    summary: str
    description: str
    tags: tuple[str, ...]
    authed: bool
    request_model: type[BaseModel] | None
    response_model: type[BaseModel] | None
    reload_gated: bool
    reads_body: bool
    error_statuses: tuple[int, ...]
    success_status: int
    additional_success_statuses: tuple[int, ...]
    success_media_types: dict[str, tuple[str, ...]]
    action: RouteAction
    # A model whose fields the emitter publishes as ``in: query`` parameters for ANY
    # method, additive to ``request_model`` (which stays a body on a write, query on a
    # read). It is the only way a WRITE-method door documents the query it reads.
    query_model: type[BaseModel] | None = None
    destructive: bool = False
    # A surface served by a MOUNTED ASGI app (an MCP transport, the sub-MCP mount),
    # recorded by :meth:`RouteRegistry.record_mounted` so the registry describes the
    # WHOLE served surface. It is not a handler route: it carries no feature tags and
    # its credential gate is the mount's own, so every consumer that describes the
    # HANDLER surface skips it and only the ones asking "is this path served, and is it
    # credential-gated?" (the rate limiter) read it.
    mounted: bool = False
    # Who registered the route (:class:`RouteOwner`): ``core`` for a native/operator
    # route, ``plugin`` for a declared plugin route. The cross-owner collision check
    # keys on it.
    owner: RouteOwner = CORE_OWNER
    # Whether the route answers UNAUTHENTICATED — the negation of ``authed``, kept as
    # the explicit declared flag the verifier's declared-public tier reads to grant the
    # public resource id regardless of owner.
    public: bool = False
    # Whether the serving router matches this route on the RAW request path (see
    # :meth:`RouteRegistry.mark_raw_path_matched` and ``HttpSurface.use_raw_path_key``),
    # so a percent-encoded slash inside a path parameter (a state record ``{key}``) stays
    # ONE segment. The access-control resolver reads this flag to fail closed: a request
    # whose canonical path carries an encoded slash may resolve ONLY to a raw-path-matched
    # route, because every other route is matched by Starlette on the decoded path and
    # authz must never reason on a different form than the router.
    raw_path_matched: bool = False
    # The justification a route with no ``{"data": <model>}`` JSON body declares in
    # place of a ``response_model`` (mutually exclusive with it — see
    # :meth:`RouteRegistry.record`). The emitter surfaces it on the None-branch success
    # response so a bodyless route is a declared, described exception, never a silent
    # empty ``data``.
    no_body_reason: str | None = None
    # Whether the JSON success body is wrapped in the ``{"data": <response_model>}``
    # envelope (the default) or is the ``response_model``'s schema DIRECTLY at the top
    # level (a RAW non-enveloped body). An unwrapped route ALWAYS carries a
    # ``response_model`` — the record guard refuses ``enveloped=False`` with a bare
    # ``None`` — so the emitter renders its 200 body as the model's ``$ref`` with no
    # ``data`` wrapper.
    enveloped: bool = True


@dataclass(frozen=True)
class _ShapeEntry:
    """One owned ``/api`` route in the shape index.

    Carries its parsed shape, the methods it is SERVED on (``GET`` implies ``HEAD``), and its
    metadata (owner + public).
    """

    shape: Shape
    methods: frozenset[str]
    meta: RouteMetadata
