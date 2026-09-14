"""The process route registry: record/match routes, stage the shape index, roll
back an owner, and audit epoch route preservation."""

from __future__ import annotations

import inspect
from dataclasses import replace

from pydantic import BaseModel
from tai42_contract.app import DeclaredRouteMetadata

from tai42_skeleton.app.route_registry.metadata import (
    CORE_OWNER,
    CrossOwnerRouteCollision,
    EpochRouteAuditError,
    RouteMetadata,
    RouteOwner,
    _ShapeEntry,
)
from tai42_skeleton.app.route_registry.route_action import (
    _JSON_MEDIA_TYPE,
    Handler,
    RouteAction,
    _handler_source,
    _resolve_route_action,
    _shape_specificity,
    _success_media_types,
    derive_route_action,
)
from tai42_skeleton.app.route_shapes import collision, overlap, parse_concrete, parse_shape


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
        # Per plugin owner, the import module(s) whose ``@custom_route`` registered its
        # routes — the module the handler is DEFINED in, which is the module whose
        # import fires the decorator. A plugin whose routes register in a SIBLING of its
        # manifest leaf (the leaf imports the sibling for the side-effect) records the
        # sibling here, so a reload can pop+reimport exactly that sibling under the
        # owner's binding and re-fire its decorators instead of leaving it cached.
        # Process-spine like ``_routes`` (never epoch-staged), so a reload reads the
        # association the live epoch registered.
        self._owner_route_modules: dict[RouteOwner, set[str]] = {}

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
        no_body_reason: str | None = None,
        enveloped: bool = True,
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
        if response_model is None:
            if not enveloped:
                raise ValueError(
                    f"route {'/'.join(methods)} {path} declares enveloped=False but no response_model — "
                    "a raw non-enveloped body still needs a real response_model to describe its schema"
                )
            if not (no_body_reason and no_body_reason.strip()):
                raise ValueError(
                    f"route {'/'.join(methods)} {path} declares no response_model and no no_body_reason — "
                    "declare a response model, or pass a non-blank no_body_reason for a route with no JSON body"
                )
        elif no_body_reason is not None:
            raise ValueError(
                f"route {'/'.join(methods)} {path} passes both a response_model and a no_body_reason — "
                "a route has a typed body OR is declared no-body, never both"
            )
        source = _handler_source(handler)
        method_key = tuple(sorted(m.upper() for m in methods))
        resolved_action = _resolve_route_action(action, method_key, path, authed)
        success_media_types: dict[str, tuple[str, ...]]
        if enveloped:
            success_media_types = _success_media_types(source, method_key)
        else:
            # An unwrapped route serves a RAW JSON body (its response_model) — the wire
            # Content-Type is application/json for every method. A download disposition a
            # JSON-file door sets (the source-derived octet-stream marker) is a delivery
            # concern, not a second body shape, so the declared JSON body wins over it.
            success_media_types = dict.fromkeys(method_key, (_JSON_MEDIA_TYPE,))
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
            success_media_types=success_media_types,
            action=resolved_action,
            destructive=destructive,
            owner=owner,
            public=public,
            no_body_reason=no_body_reason,
            enveloped=enveloped,
        )
        self._record_shape(meta, method_key)
        self._routes[path, method_key] = meta
        if owner.kind == "plugin":
            # Remember which import module registered this plugin route, keyed on the
            # owner a reload resolves from its mount binding, so the reload re-fires the
            # right module even when it is a sibling of the manifest leaf. Attribution
            # assumes the handler is DEFINED in the module whose import registers it: a
            # route registered from a module that does not define its handler is not
            # re-fired on reload and the epoch-build audit fails the reload loudly.
            self._owner_route_modules.setdefault(owner, set()).add(handler.__module__)
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

    def mark_raw_path_matched(self, path_prefix: str) -> None:
        """Mark every recorded route whose template starts with ``path_prefix`` as
        raw-path-matched, so the access-control resolver knows these routes — and ONLY
        these — may resolve a request whose canonical path carries an encoded slash.

        Called by ``HttpSurface.use_raw_path_key`` alongside the served-router upgrade, so
        the authz metadata and the serving router are marked at the SAME seam and can never
        diverge. Idempotent, and re-applied every epoch (a reload re-imports the routers
        and re-calls ``use_raw_path_key``), so a route re-registered by a reload is re-marked.
        """
        for key, meta in list(self._routes.items()):
            if meta.path.startswith(path_prefix) and not meta.raw_path_matched:
                self._routes[key] = replace(meta, raw_path_matched=True)
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
            # Strict ``>`` keeps the FIRST-registered entry on an equal-specificity,
            # same-owner, same-method overlap, and this MUST equal Starlette's first-match:
            # the declared-public tier reads this match, so the authed route of such an
            # overlap must be registered before the public one, else an authed route resolves
            # public.
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
        # Drop the owner's recorded route module(s) too: a rolled-back owner registers
        # nothing, so a later reload must not pop+reimport a module attributed to a run
        # that left no route standing.
        self._owner_route_modules.pop(owner, None)
        self._version += 1

    def owner_route_modules(self, owner: RouteOwner) -> frozenset[str]:
        """The import module(s) that registered ``owner``'s routes on the live epoch —
        the modules a reload must pop+reimport under the owner's binding so their
        ``@custom_route`` decorators re-fire. Empty for an owner that registered no
        route yet (a first boot, or a never-loaded plugin). The reload uses this to
        re-fire ONLY the owner's own route-registering module(s), never a wider set."""
        return frozenset(self._owner_route_modules.get(owner, frozenset()))

    def audit_plugin_routes_preserved(self, expected_owners: set[RouteOwner]) -> None:
        """Guard the epoch rebuild against a SILENT plugin-route unmount.

        An epoch build re-imports the manifest modules to re-fire their route
        registrations into the STAGED generation. A plugin whose route lives in a
        sibling of its manifest leaf can silently fail to re-register (the sibling
        stayed cached in ``sys.modules``), dropping every one of that plugin's routes
        from the generation about to be committed — a 404 until process restart.

        Called during the build, BEFORE the atomic commit, with ``expected_owners``
        the plugin owners the NEW manifest still declares routes for. For each such
        owner that served a route in the live (committed) generation, the staged
        generation must serve at least one — else this raises so the build discards
        the staged generation and the OLD epoch keeps serving.

        Precise by construction: a plugin dropped from the manifest, or one whose new
        spec declares no routes, is not in ``expected_owners`` and never trips the
        guard; a remap or an update that keeps at least one route still passes (an
        owner's routes are all in one binding, so the sibling-cache bug drops them
        all-or-nothing). A no-op outside an epoch build (no staged generation)."""
        if self._staged_shapes is None:
            return
        staged_owners = {entry.meta.owner for entry in self._staged_shapes}
        committed_plugin_owners = {
            entry.meta.owner for entry in self._committed_shapes if entry.meta.owner.kind == "plugin"
        }
        dropped = sorted(
            f"{owner.owner_ref}:{owner.item_name}"
            for owner in committed_plugin_owners & expected_owners
            if owner not in staged_owners
        )
        if dropped:
            raise EpochRouteAuditError(
                "epoch rebuild dropped every HTTP route of plugin(s) still declared in the manifest: "
                f"{dropped} — their route-registering module did not re-register on reload; refusing to "
                "commit a route-dropping epoch (the previous epoch keeps serving)"
            )


# The one process-wide registry. ``HttpSurface.custom_route`` records into it;
# the emitter and the parity gate read it via ``load_api_routes``.
route_registry = RouteRegistry()
