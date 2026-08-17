"""The route-metadata registry — the single source of truth for the app's
self-describing HTTP surface.

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

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.app import DeclaredRouteMetadata

from tai42_skeleton.app.route_shapes import Literal as ShapeLiteral
from tai42_skeleton.app.route_shapes import Shape, collision, overlap, parse_concrete, parse_shape

Handler = Callable[[Request], Awaitable[Response]]


class CrossOwnerRouteCollision(RuntimeError):
    """A route registration whose shape+methods collide with a DIFFERENT owner's
    already-registered route. Raised to kill silent registry shadowing by
    construction — one owner per route, enforced at registration."""


# The route action-class — the SINGLE authoritative source of a route's
# authorization character:
#
# * ``read`` / ``write`` are the GRANTABLE classes: a role's per-tag level on the
#   route's feature tag decides reach, and the class equals the method's derived
#   action (:func:`method_to_action`).
# * ``fenced`` is the admin-only MUTATION fence: no per-tag level opens it, admin
#   only. It is DISTINCT from the ``RouteMetadata.destructive`` spec-surface bool
#   (which the adapter auto-forces on every DELETE) — sourcing the fence from that
#   bool would over-fence editor-reachable DELETEs, so the two never interact.
# * ``secret`` is the admin-only bulk-secret READ fence: a GET whose payload is
#   admin-equivalent, admin only and non-grantable like ``fenced``.
RouteAction = Literal["read", "write", "fenced", "secret"]
_VALID_ROUTE_ACTIONS: frozenset[str] = frozenset(("read", "write", "fenced", "secret"))
_GRANTABLE_ROUTE_ACTIONS: frozenset[str] = frozenset(("read", "write"))
_FENCED_ROUTE_ACTIONS: frozenset[str] = frozenset(("fenced", "secret"))

_READ_METHODS: frozenset[str] = frozenset(("GET", "HEAD", "OPTIONS"))
_WRITE_METHODS: frozenset[str] = frozenset(("POST", "PUT", "PATCH", "DELETE"))

# Every method a Starlette ``Mount`` claims beneath its prefix: a mount dispatches on
# the path alone, so the mounted app answers each of them (with its own 404/405) and no
# handler route that also matches the path ever sees them. ``HEAD`` is left out because
# every consumer derives it from ``GET``.
MOUNT_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def method_to_action(method: str) -> Literal["read", "write"]:
    """Map an HTTP method to its derived action-class — the ONE place the read/write
    action is derived from the method. ``GET``/``HEAD``/``OPTIONS`` → ``read``;
    ``POST``/``PUT``/``PATCH``/``DELETE`` → ``write``.

    An unknown/empty method raises loudly (fail-closed) — the derivation never
    defaults to ``read``, so an unclassifiable method is caught at registration/boot
    rather than silently admitted as a read."""
    upper = method.upper()
    if upper in _READ_METHODS:
        return "read"
    if upper in _WRITE_METHODS:
        return "write"
    raise ValueError(f"unclassifiable HTTP method {method!r}: cannot derive a read/write action")


def derive_route_action(methods: tuple[str, ...]) -> Literal["read", "write"]:
    """The grantable action-class a route's method set derives to: ``write`` when the
    route serves ANY write method, else ``read``. Enforcement re-derives per-method
    from the live request method (:func:`method_to_action`), so this coarse label
    only classes the route as a whole (the UI grouping and the boot validation)."""
    return "write" if any(method_to_action(method) == "write" for method in methods) else "read"


# Success content types, derived from markers in the handler source. The default
# JSON surface answers the ``{"data": ...}`` envelope; a streaming, CSV, HTML, or
# asset-serving route answers its own media type instead, which the emitter must
# document faithfully (no ``{"data": ...}`` wrapper). Each marker is a token whose
# presence in the (method-scoped) handler source — its own body or the name of a
# shared responder it calls — contributes that media type. A method that matches
# several markers documents several content types (the runs export serves CSV or a
# JSON download from one method); a method that matches none answers JSON.
_JSON_MEDIA_TYPE = "application/json"
_MEDIA_TYPE_MARKERS = (
    ("text/event-stream", "text/event-stream"),
    ("text/csv", "text/csv"),
    ("_csv_response", "text/csv"),
    ("asset_content_type", "application/octet-stream"),
    ("HTML_CONTENT_TYPE", "text/html"),
    # The interactions callback door delegates its GET branch to ``_callback_get``,
    # which serves the browser confirm page; the delegated responder's name marks
    # the HTML surface (the derivation follows the responder the handler calls, not
    # only its own inline responses).
    ("_callback_get", "text/html"),
    # A downloadable attachment (the backup export, and the runs export's JSON
    # format) — a file, not the enveloped JSON surface.
    ("Content-Disposition", "application/octet-stream"),
)

# Marks the branch of a multi-method handler that dispatches on the request method,
# so each method's success media type is derived from only the code that serves it.
_METHOD_GUARD = re.compile(r"""request\.method\s*==\s*['"]([A-Za-z]+)['"]""")


@dataclass(frozen=True)
class RouteOwner:
    """Who registered a route: ``core`` for a native/operator route, ``plugin``
    for a declared plugin route (then ``owner_ref`` is the ``namespace/name``
    listing and ``item_name`` the provided item). The identity the cross-owner
    collision check compares — one owner per route shape."""

    kind: Literal["core", "plugin"] = "core"
    owner_ref: str | None = None
    item_name: str | None = None


CORE_OWNER = RouteOwner(kind="core")


@dataclass(frozen=True)
class RouteMetadata:
    """One self-describing route: its wire shape plus the OpenAPI metadata the
    emitter and the coverage/parity gates consume."""

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
    # keys on it, and the verifier's declared-public tier grants only a ``plugin``
    # owner's public route.
    owner: RouteOwner = CORE_OWNER
    # Whether the route answers UNAUTHENTICATED — the negation of ``authed``, kept as
    # the explicit declared flag the verifier's declared-public tier reads.
    public: bool = False


def _handler_source(func: Callable[..., object]) -> str:
    return inspect.getsource(func)


def _method_scoped_source(source: str, method: str) -> str:
    """The handler source as ``method`` sees it: shared lines plus the block guarded
    by ``if request.method == "<method>"``, dropping the blocks that guard a
    DIFFERENT method. A handler that never dispatches on ``request.method`` (the
    common case) yields its whole source unchanged, so single-method routes and
    multi-method routes that share one code path are untouched."""
    kept: list[str] = []
    foreign_indent: int | None = None
    for line in source.splitlines(keepends=True):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if foreign_indent is not None:
            if stripped and indent <= foreign_indent:
                foreign_indent = None  # the block guarding another method has closed
            else:
                continue  # still inside a block that serves a different method
        guard = _METHOD_GUARD.search(line) if stripped.startswith(("if ", "elif ")) else None
        if guard is not None and guard.group(1).upper() != method.upper():
            foreign_indent = indent
            continue
        kept.append(line)
    return "".join(kept)


def _method_media_types(source: str) -> tuple[str, ...]:
    matched = tuple(dict.fromkeys(media for token, media in _MEDIA_TYPE_MARKERS if token in source))
    return matched or (_JSON_MEDIA_TYPE,)


def _success_media_types(source: str, methods: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Map each method to the content type(s) its success response serves, derived
    from the method-scoped handler source so a route whose methods answer different
    media types (the callback door: GET serves HTML, POST serves the JSON envelope)
    documents each method faithfully."""
    return {method: _method_media_types(_method_scoped_source(source, method)) for method in methods}


def _resolve_route_action(action: RouteAction | None, methods: tuple[str, ...], path: str, authed: bool) -> RouteAction:
    """The route's authoritative action-class. Every AUTHED route DECLARES its class
    explicitly: a grantable ``read``/``write``, or the admin-only ``fenced``/``secret``
    fence. A declared ``read``/``write`` is VALIDATED against every method's derived action,
    so a misdeclared class (``read`` on a write method) is refused at registration. An
    authed route that declares NOTHING BOOT-FAILS here — auto-deriving read/write for an
    undeclared authed route is fail-open (a forgotten fence silently becomes grantable), so
    the omission is refused rather than divined. A PUBLIC route (``authed=False``) never
    enforces an action, so it is exempt and its class is derived from the method for the
    spec — and a ``fenced``/``secret`` class on a public route is itself a contradiction
    (the fence is enforced only in the authenticated path, so a public fence silently opens
    an admin-only door), refused here symmetric with the authed-without-action raise. An
    unknown method raises out of the derivation (fail-closed)."""
    if action in _FENCED_ROUTE_ACTIONS:
        if not authed:
            raise ValueError(
                f"public route {'/'.join(methods)} {path} declares action={action!r}; a fence is enforced only "
                "in the authenticated path, so a fenced/secret class on an authed=False route silently opens it — "
                "a public route must be read/write (or authed=True to keep the fence)"
            )
        return action  # type: ignore[return-value]
    derived = derive_route_action(methods)
    if action is None:
        if authed:
            raise ValueError(
                f"authed route {'/'.join(methods)} {path} declares no action-class; every authed route must "
                "declare read/write/fenced/secret explicitly — allow-by-omission is a fail-open fence"
            )
        return derived
    if action not in _GRANTABLE_ROUTE_ACTIONS:
        raise ValueError(f"route {'/'.join(methods)} {path} declares unknown action {action!r}")
    # An explicit read/write must match the method-derived class for EVERY method.
    for method in methods:
        if method_to_action(method) != action:
            raise ValueError(
                f"route {'/'.join(methods)} {path} declares action={action!r} but method {method!r} "
                f"derives {method_to_action(method)!r}; a grantable route's class must equal its method"
            )
    return action


@dataclass(frozen=True)
class _ShapeEntry:
    """One owned ``/api`` route in the shape index: its parsed shape, the methods
    it is SERVED on (``GET`` implies ``HEAD``), and its metadata (owner + public)."""

    shape: Shape
    methods: frozenset[str]
    meta: RouteMetadata


class RouteRegistry:
    """In-memory map of every registered route, keyed by ``(path, methods)``.

    Populated as a side effect of importing the router modules. Recording the
    same ``(path, methods)`` twice replaces the entry (a module re-import is
    idempotent), never accumulates duplicates.

    Alongside the append-only ``(path, methods)`` map the registry keeps a
    generation-scoped SHAPE INDEX of every ``/api`` route by parsed shape + served
    methods + owner. The index answers the cross-owner collision check at
    registration and the concrete-path ownership :meth:`match` the verifier's
    declared-public tier reads. Unlike ``_routes`` (a process-spine dedup map that
    is never rolled back), the shape index is STAGED per epoch build and committed
    atomically, so an uninstalled/remapped route leaves it the instant its module
    stops re-registering — the match and collision answers reflect exactly the
    generation the build assembled.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, tuple[str, ...]], RouteMetadata] = {}
        self._version = 0
        self._committed_shapes: list[_ShapeEntry] = []
        self._staged_shapes: list[_ShapeEntry] | None = None

    @property
    def version(self) -> int:
        """Bumped by every :meth:`record`, so a consumer that derives a table from the
        registry (the rate limiter's public-door coverage) can memoize against it and
        rebuild when a reload re-records the surface. A re-import that records the
        SAME metadata still bumps it — a rebuild is idempotent, a stale table is not."""
        return self._version

    def record(
        self,
        *,
        path: str,
        methods: list[str],
        name: str | None,
        handler: Handler,
        summary: str,
        tags: list[str],
        authed: bool,
        request_model: type[BaseModel] | None,
        response_model: type[BaseModel] | None,
        query_model: type[BaseModel] | None = None,
        destructive: bool = False,
        action: RouteAction | None = None,
        declared: DeclaredRouteMetadata | None = None,
        owner: RouteOwner = CORE_OWNER,
        public: bool = False,
    ) -> None:
        """Record one route's metadata.

        A route in the ``/api/*`` spec surface supplies ``declared`` — the
        operation adapter passes an operation's metadata, and a native handler
        passes its own — so its behavioral properties come from a declaration,
        never a divination. A handler that declares nothing (a route outside the
        spec surface) records trivial defaults, since its behavioral metadata is
        never emitted. The per-method success media type is always derived from
        the handler source. Raises loudly on a missing minimum-bar field so a
        route that fails to self-describe is caught at import, not in the gate."""
        if not summary:
            raise ValueError(f"route {'/'.join(methods)} {path} is missing a non-empty summary")
        if not tags:
            raise ValueError(f"route {'/'.join(methods)} {path} is missing at least one tag")
        source = _handler_source(handler)
        method_key = tuple(sorted(m.upper() for m in methods))
        resolved_action = _resolve_route_action(action, method_key, path, authed)
        if declared is None:
            reload_gated = False
            reads_body = False
            error_statuses: tuple[int, ...] = ()
            success_status = 200
            additional_success_statuses: tuple[int, ...] = ()
        else:
            reload_gated = declared.reload_gated
            reads_body = declared.reads_body
            error_statuses = declared.error_statuses
            success_status = declared.success_status
            additional_success_statuses = declared.additional_success_statuses
        meta = RouteMetadata(
            path=path,
            methods=method_key,
            name=name or handler.__name__,
            summary=summary,
            description=inspect.cleandoc(handler.__doc__ or ""),
            tags=tuple(tags),
            authed=authed,
            request_model=request_model,
            response_model=response_model,
            query_model=query_model,
            reload_gated=reload_gated,
            reads_body=reads_body,
            error_statuses=error_statuses,
            success_status=success_status,
            additional_success_statuses=additional_success_statuses,
            success_media_types=_success_media_types(source, method_key),
            action=resolved_action,
            destructive=destructive,
            owner=owner,
            public=public,
        )
        self._record_shape(meta, method_key)
        self._routes[path, method_key] = meta
        self._version += 1

    def record_mounted(self, *, path: str, methods: list[str], name: str, summary: str) -> None:
        """Record one path TEMPLATE served by a mounted ASGI app — an MCP transport
        route, the sub-MCP mount — as it is mounted, so the registry describes the whole
        served surface instead of leaving these paths to whichever handler route happens
        to also match them (the Studio SPA catch-all matches every GET).

        Always ``authed=True``: a mount serves protocol traffic behind its own credential
        gate, never a declared public door, so the flood limiter passes it through
        untouched rather than charging it to the public catch-all's family. It carries no
        feature tags, no models and no declared OpenAPI metadata — it self-describes
        nothing — and its ``mounted`` flag keeps it out of the handler-surface consumers
        (the per-tag role gate, the SPA-shell reserved derivation and its boot audit, the
        route listing the Roles page joins). The action-class is derived from the methods
        for completeness; no gate enforces it.

        A mounted record states what is mounted NOW, so re-mounting (every epoch rebuilds
        the serving app) replaces EVERY mounted record for the path — keyed on the path
        alone, unlike a handler registration — and bumps the version. Keying it on the
        method set as well would accumulate: an epoch that mounts a narrower set would
        leave the previous epoch's wider record standing beside the new one, still
        claiming methods this deployment no longer serves.
        """
        method_key = tuple(sorted(m.upper() for m in methods))
        for stale in [key for key, meta in self._routes.items() if key[0] == path and meta.mounted]:
            del self._routes[stale]
        self._routes[path, method_key] = RouteMetadata(
            path=path,
            methods=method_key,
            name=name,
            summary=summary,
            description="",
            tags=(),
            authed=True,
            request_model=None,
            response_model=None,
            reload_gated=False,
            reads_body=False,
            error_statuses=(),
            success_status=200,
            additional_success_statuses=(),
            success_media_types={},
            action=derive_route_action(method_key),
            mounted=True,
        )
        self._version += 1

    def routes(self) -> list[RouteMetadata]:
        """Every recorded route, ordered by path then methods for a stable spec."""
        return [self._routes[key] for key in sorted(self._routes)]

    # -- shape index (cross-owner collision + concrete-path ownership) ----------

    @staticmethod
    def _served_methods(method_key: tuple[str, ...]) -> frozenset[str]:
        """The methods a route is served on — ``GET`` implies ``HEAD`` (Starlette
        adds it), so a public GET route answers HEAD probes on the same tier."""
        declared = frozenset(method_key)
        return declared | {"HEAD"} if "GET" in declared else declared

    def _shape_target(self) -> list[_ShapeEntry]:
        """The write-target shape list: the staged generation during an epoch build,
        else the committed live one (cold boot writes committed directly)."""
        return self._staged_shapes if self._staged_shapes is not None else self._committed_shapes

    def _record_shape(self, meta: RouteMetadata, method_key: tuple[str, ...]) -> None:
        """Index one ``/api`` handler route by shape, raising on a cross-owner
        collision. Mounted surfaces (their own credential gate) and non-``/api``
        routes (governed by the SPA-shell tier) are outside the ownership space."""
        if meta.mounted or not meta.path.startswith("/api/"):
            return
        shape = parse_shape(meta.path)
        served = self._served_methods(method_key)
        target = self._shape_target()
        for entry in target:
            if entry.meta.owner != meta.owner and collision(shape, served, entry.shape, entry.methods):
                raise CrossOwnerRouteCollision(
                    f"route {'/'.join(method_key)} {meta.path} (owner {meta.owner}) collides with "
                    f"{'/'.join(sorted(entry.methods))} {entry.meta.path} (owner {entry.meta.owner}) — "
                    "one owner per route shape; remap the mount base to resolve"
                )
        target[:] = [e for e in target if not (e.meta.path == meta.path and e.methods == served)]
        target.append(_ShapeEntry(shape=shape, methods=served, meta=meta))

    def api_shape_index(self) -> list[_ShapeEntry]:
        """The committed ``/api`` shape generation — each entry's parsed shape,
        served methods, and owning metadata. The marketplace install pre-flight and
        preview door read it to collision-check a candidate route against exactly
        the ownership the live epoch serves (unlike :meth:`routes`, whose dedup map
        keeps an uninstalled plugin's stale entry)."""
        return list(self._committed_shapes)

    def match(self, path: str, method: str) -> RouteMetadata | None:
        """The registered ``/api`` route that OWNS the concrete request ``(path,
        method)``, or ``None``. Deterministic: cross-owner shapes never overlap, so
        at most one owner matches; among a single owner's overlapping shapes the
        most specific (most literal segments) wins."""
        concrete = parse_concrete(path)
        best: _ShapeEntry | None = None
        for entry in self._committed_shapes:
            if method not in entry.methods or not overlap(concrete, entry.shape):
                continue
            if best is None or _shape_specificity(entry.shape) > _shape_specificity(best.shape):
                best = entry
        return best.meta if best is not None else None

    def begin_shape_staging(self) -> None:
        """Open a fresh staged shape generation for an epoch build; the committed
        live one keeps answering match/collision until the atomic commit."""
        self._staged_shapes = []

    def commit_shape_staging(self) -> None:
        """Promote the staged shape generation to committed — one reference flip in
        the build's no-await swap stretch."""
        if self._staged_shapes is not None:
            self._committed_shapes = self._staged_shapes
            self._staged_shapes = None

    def abort_shape_staging(self) -> None:
        """Drop the staged shape generation on a failed build; the committed live
        one is untouched."""
        self._staged_shapes = None

    def reset_shape_index(self) -> None:
        """Clear the write-target shape generation before a registration pass
        re-records it — the staged one during a build, the committed one at boot."""
        self._shape_target().clear()

    def rollback_owner(self, owner: RouteOwner) -> None:
        """Deregister EVERY route this owner recorded — from both the process-spine
        dedup map (``_routes``) and the write-target shape generation — so a module
        whose import/bind/verify failed leaves no trace the match/collision surface or
        the OpenAPI enumeration can see. Paired by the caller with the FastMCP
        route-table rollback so all three surfaces drop the module together.

        Keyed on the plugin owner identity, which is one-to-one with a bound module,
        so it removes exactly that module's rows (including any stale prior-epoch
        ``_routes`` entry the failed pass did not re-record). Refuses the core owner:
        core routes share one owner and are not owner-isolable, so a core rollback
        would nuke the whole native surface — a misuse."""
        if owner.kind != "plugin":
            raise ValueError(f"rollback_owner refuses the {owner.kind} owner — only a plugin owner is rollable")
        self._routes = {key: meta for key, meta in self._routes.items() if meta.owner != owner}
        target = self._shape_target()
        target[:] = [entry for entry in target if entry.meta.owner != owner]
        self._version += 1


def _shape_specificity(shape: Shape) -> int:
    """A shape's specificity for match tie-breaking: its count of LITERAL segments
    (a fixed segment out-ranks any template), so ``/api/x/y`` beats ``/api/x/{z}``
    when both match a concrete path."""
    return sum(1 for segment in shape if isinstance(segment, ShapeLiteral))


# The one process-wide registry. ``HttpSurface.custom_route`` records into it;
# the emitter and the parity gate read it via ``load_api_routes``.
route_registry = RouteRegistry()


class _SpecFastMCP:
    """A no-op stand-in for FastMCP's ``custom_route`` used only for OFFLINE
    metadata capture — it returns the handler unchanged, so importing the router
    modules records their metadata without a booted server."""

    def custom_route(
        self, path: str, methods: list[str], name: str | None, include_in_schema: bool
    ) -> Callable[[Handler], Handler]:
        return lambda fn: fn


class _SpecLifecycle:
    """A no-op stand-in for the app's ``lifecycle`` seam used only for OFFLINE
    metadata capture — a router module registering a startup/shutdown/reload
    handler at import time gets the handler back unchanged, so no handler is
    wired and no server is needed."""

    def on_startup(self, func: Callable[..., object]) -> Callable[..., object]:
        return func

    def on_shutdown(self, func: Callable[..., object]) -> Callable[..., object]:
        return func

    def on_reload(self, func: Callable[..., object]) -> Callable[..., object]:
        return func

    def on_post_swap(self, func: Callable[..., object]) -> Callable[..., object]:
        return func


class _SpecApp:
    """Minimal ``tai42_app`` impl exposing only the ``http`` and ``lifecycle`` seams
    the router modules touch at import time, so metadata capture needs no database,
    Redis, or config."""

    def __init__(self) -> None:
        from tai42_skeleton.app.http import HttpSurface

        self._fast_mcp = _SpecFastMCP()
        self.http = HttpSurface(self)  # type: ignore[arg-type]
        self.lifecycle = _SpecLifecycle()

    def effective_router_modules(self) -> None:
        """No deployment is served under the spec harness, so the enumeration
        universe is the whole ``tai42_skeleton.routers`` package — signalled by
        ``None`` (never a curated started-manifest set)."""
        return None


class _RouterUniverseSource(Protocol):
    """The one method the shared importer asks of the currently-bound app to choose its
    enumeration universe. The forwarding ``tai42_app`` handle is typed as the assembled
    facade (which carries only namespace facets), so this Protocol types the flat method
    the handle forwards to the bound impl — the started ``TaiMCP`` (answers its manifest's
    effective router set) and the offline ``_SpecApp`` (answers ``None``) both satisfy it."""

    def effective_router_modules(self) -> list[str] | None: ...


def _started_router_modules() -> list[str] | None:
    """The EFFECTIVE router set of a started deployment, or ``None`` when none answers one
    (unbound process, offline spec harness, partially-faked app). The probe must stay a
    ``hasattr`` on the forwarding handle — a facet probe reads a partially-faked app as
    unbound."""
    from tai42_contract.app import tai42_app

    if not hasattr(tai42_app, "effective_router_modules"):
        return None
    return cast("_RouterUniverseSource", tai42_app).effective_router_modules()


def _import_all_router_modules() -> None:
    """Import EVERY module under ``tai42_skeleton.routers`` — the whole-package
    enumeration universe used only offline (CLI spec/parity tools, unbooted tests)."""
    import importlib
    import pkgutil

    import tai42_skeleton.routers as routers_pkg

    for module_info in pkgutil.iter_modules(routers_pkg.__path__, routers_pkg.__name__ + "."):
        importlib.import_module(module_info.name)


def _ensure_routers_imported() -> None:
    """Import the router modules that define the enumeration universe.

    There are exactly two universes. In a STARTED process the bound app answers its
    manifest's EFFECTIVE router set, and THAT set is the universe: ``start()`` already
    imported those modules, so re-importing them is an idempotent no-op and no other
    router is pulled into the live route table. Offline — an unbound CLI/test process,
    or a process whose bound impl answers no router set — the whole
    ``tai42_skeleton.routers`` package is the universe, so an offline enumeration sees
    every route with no server to boot.

    A router module resolves ``tai42_app.http.custom_route`` at import, so the offline
    import runs under the ``_SpecApp`` stand-in bound for that import ALONE — this
    enumeration is a read and must leave the process's app binding as it found it.
    """
    from tai42_contract.app import tai42_app

    effective = _started_router_modules()
    if effective is not None:
        # A STARTED deployment: its effective router set IS the universe.
        # start() already imported these; re-import is an idempotent no-op.
        import importlib

        for module in effective:
            importlib.import_module(module)
        return
    with tai42_app.bound(_SpecApp()):
        _import_all_router_modules()


def load_api_routes() -> list[RouteMetadata]:
    """The shared route-enumeration primitive: every registered ``/api/*`` route.

    Imports the enumeration universe if needed, then returns its metadata. In a STARTED
    process that universe is the deployment's effective router set (so this enumerates
    exactly the served surface); offline it is the whole router package. Both the OpenAPI
    emitter/coverage gate and the CLI↔route parity gate call this so they enumerate the
    API surface identically.
    """
    _ensure_routers_imported()
    return [meta for meta in route_registry.routes() if meta.path.startswith("/api/")]


def load_all_routes() -> list[RouteMetadata]:
    """Every registered route — the whole self-describing HTTP surface, ``/api/*`` and
    the non-``/api`` operational routes (``/health``, ``/ready``, …) alike.

    Imports the enumeration universe if needed, then returns its metadata. In a STARTED
    process that universe is the deployment's effective router set, so this enumerates
    exactly the served surface and never pulls an un-mounted router into the live route
    table; offline it is the whole router package. The access-control resolver derives its
    SPA-shell reserved set from the non-``/api`` GET routes surfaced here, so a route added
    to a router joins the reserved set with no static list to maintain.
    """
    _ensure_routers_imported()
    return route_registry.routes()


def route_action_violations() -> list[str]:
    """Every GATED route whose action-class fails the audit — the boot gate's fail
    list (empty means clean). A route is an offender when its ``action`` is not one of
    the four valid classes (allow-by-omission is dead) or, for a grantable
    ``read``/``write`` route, when the declared class disagrees with the method-derived
    action. ``fenced``/``secret`` are the explicit admin-only fence classes and are
    exempt from the method-equals-action rule. Public (``authed=False``) routes are not
    gated — their action never enforces — so they are not audited here.

    Enumerates through :func:`load_all_routes` so the enumeration universe is imported
    before the audit runs — in a started process that is the deployment's served router
    surface, so the audit judges exactly what the deployment serves; iterating the raw
    registry could pass VACUOUSLY (an empty loop is a silent no-op) had the routers not
    yet been imported."""
    violations: list[str] = []
    for meta in load_all_routes():
        if not meta.authed:
            continue
        if meta.action not in _VALID_ROUTE_ACTIONS:
            violations.append(f"{'/'.join(meta.methods)} {meta.path}: unclassified action {meta.action!r}")
            continue
        if meta.action in _GRANTABLE_ROUTE_ACTIONS:
            try:
                derived = derive_route_action(meta.methods)
            except ValueError as exc:
                violations.append(f"{'/'.join(meta.methods)} {meta.path}: {exc}")
                continue
            if derived != meta.action:
                violations.append(
                    f"{'/'.join(meta.methods)} {meta.path}: declared action={meta.action!r} "
                    f"disagrees with the method-derived {derived!r}"
                )
    return violations
