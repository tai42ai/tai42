"""HTTP delivery surface — the impl body behind the ``app.http`` facet.

Owns the ASGI middleware stack registered via ``@app.http.middleware`` and the
custom-route passthrough; :meth:`finalize` applies the stack (plus the sub-MCP
mount) around whichever ASGI app the launch surface builds.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response

from tai42_skeleton.app.mount_map import MountBinding, MountRegistrationError, current_mount_binding, note_registered
from tai42_skeleton.app.route_registry import CORE_OWNER, MOUNT_METHODS, RouteOwner, route_registry

if TYPE_CHECKING:
    from tai42_contract.app import DeclaredRouteMetadata

    from tai42_skeleton.app.route_registry import RouteAction
    from tai42_skeleton.app.server import TaiMCP


def plugin_owner(binding: MountBinding) -> RouteOwner:
    """The route owner identity a declared plugin route records under — one per bound
    module. The SINGLE source of that identity, shared by ``custom_route`` (which
    stamps it on each recorded row), the rollback (which deregisters by it), and the
    reload's route-preservation audit (which resolves expected owners through it), so
    they never drift."""
    return RouteOwner(kind="plugin", owner_ref=binding.owner_ref, item_name=binding.item_name)


def record_sub_mcp_mount(prefix: str) -> None:
    """Record the sub-MCP router's served surface — everything BENEATH the mount prefix,
    which is what a Starlette ``Mount`` serves — as a mounted, credential-gated one, so
    the registry describes it instead of leaving its GETs to the Studio SPA catch-all
    that also matches them (see :meth:`RouteRegistry.record_mounted`)."""
    route_registry.record_mounted(
        path=f"{prefix.rstrip('/')}/{{path:path}}",
        methods=MOUNT_METHODS,
        name="sub_mcp_mount",
        summary="Sub-MCP app mount",
    )


class HttpSurface:
    """Middleware + custom-route registration over the app's FastMCP server."""

    def __init__(self, app: "TaiMCP") -> None:
        self._app = app
        # Keyed by the middleware class's qualified name so a module re-import
        # (each start() re-imports the middleware modules) replaces rather than
        # accumulates its entry — insertion order, hence stack order, is kept —
        # while a construction-time (build_app) middleware persists across
        # reloads.
        self._middlewares: dict[str, Middleware] = {}

    def _register_middleware(self, cls: type, options: dict[str, Any]) -> None:
        # starlette's ``Middleware`` over-constrains ``cls`` to its private
        # ``_MiddlewareFactory`` protocol; a user middleware class is a valid
        # factory at runtime.
        key = f"{cls.__module__}.{cls.__qualname__}"
        self._middlewares[key] = Middleware(cast(Any, cls), **options)

    def middleware(self, cls: type | None = None, **options: Any):
        if cls and inspect.isclass(cls):
            self._register_middleware(cls, options)
            return cls

        def decorator(inner_cls):
            self._register_middleware(inner_cls, options)
            return inner_cls

        return decorator

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
        *,
        summary: str,
        tags: list[str],
        response_model: type[BaseModel] | None,
        request_model: type[BaseModel] | None = None,
        query_model: type[BaseModel] | None = None,
        authed: bool | None = None,
        destructive: bool = False,
        action: "RouteAction | None" = None,
        declared: "DeclaredRouteMetadata | None" = None,
    ) -> Callable[[Callable[[Request], Awaitable[Response]]], Callable[[Request], Awaitable[Response]]]:
        """Register the handler with FastMCP AND record its OpenAPI metadata.

        The route serves exactly as before; the added metadata (summary, tags,
        request/response models, auth) is the source of truth the OpenAPI emitter
        and its coverage gate read. See :class:`tai42_contract.app.facets.AppHttp`.

        A route registered through the operations adapter passes ``destructive``
        (emitted as ``x-destructive``) and ``declared`` (its behavioral
        properties, taken from the operation's metadata rather than divined from
        the adapter closure's source).

        When the importing module carries a mount binding (a declared plugin
        route), ``path`` is RELATIVE and the route resolves from the declaration:
        the served path is ``/api/`` + the mount base + ``path``, ``authed`` is the
        negation of the declared ``public`` flag, and the owner is the plugin. A
        row not declared in the plugin's ``tai-plugin.yml`` — or an explicit
        ``authed`` argument on a declared route, or any route from a route-less
        item's module — is a registration error. Off a binding (a core/operator
        route) ``path`` is absolute and ``authed`` defaults to ``True``.
        """
        binding = current_mount_binding()
        if binding is not None:
            if binding.forbidden:
                raise MountRegistrationError(
                    f"item {binding.item_name!r} of plugin {binding.owner_ref!r} declares no routes in "
                    f"tai-plugin.yml but registers {'/'.join(methods)} {path}"
                )
            if authed is not None:
                raise MountRegistrationError(
                    f"declared plugin route {'/'.join(methods)} {path} (item {binding.item_name!r}) passed an "
                    "explicit authed= — the tai-plugin.yml public flag is the single source of that decision"
                )
            method_set = frozenset(m.upper() for m in methods)
            decl = binding.find_route(path, method_set)
            if decl is None:
                raise MountRegistrationError(
                    f"plugin {binding.owner_ref!r} item {binding.item_name!r} registers route "
                    f"{'/'.join(methods)} {path} not declared in its tai-plugin.yml routes"
                )
            note_registered(path, method_set)
            resolved_path = binding.resolved_path(path)
            resolved_authed = not decl.public
            resolved_public = decl.public
            owner = plugin_owner(binding)
        else:
            resolved_path = path
            resolved_authed = True if authed is None else authed
            resolved_public = not resolved_authed
            owner = CORE_OWNER

        fastmcp_route = self._app._fast_mcp.custom_route(resolved_path, methods, name, include_in_schema)

        def decorator(fn: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
            route_registry.record(
                path=resolved_path,
                methods=methods,
                name=name,
                handler=fn,
                summary=summary,
                tags=tags,
                authed=resolved_authed,
                request_model=request_model,
                response_model=response_model,
                query_model=query_model,
                destructive=destructive,
                action=action,
                declared=declared,
                owner=owner,
                public=resolved_public,
            )
            return fastmcp_route(fn)

        return decorator

    def mount_base(self) -> str:
        """The resolved absolute mount base of the module importing now — ``/api/``
        plus the mount binding's base, no trailing slash. Raises off a binding (a
        core or operator-authored module) or from a route-less item's module —
        neither owns a declared mount. See :class:`tai42_contract.app.facets.AppHttp`.
        """
        binding = current_mount_binding()
        if binding is None:
            raise MountRegistrationError(
                "mount_base() called with no mount binding present — a core or operator-authored module "
                "owns no declared mount base"
            )
        if binding.forbidden:
            raise MountRegistrationError(
                f"item {binding.item_name!r} of plugin {binding.owner_ref!r} declares no routes in "
                "tai-plugin.yml, so it has no mount base"
            )
        return binding.resolved_path("")

    def route_table_savepoint(self) -> int:
        """The current length of the FastMCP additional-route table — a savepoint the
        additive-plugin import captures BEFORE a bound module imports. A module import
        only ever APPENDS routes (one per ``custom_route``), so truncating back to this
        length drops exactly the routes that module registered and nothing earlier.

        Reaches the FastMCP-private route list because FastMCP affords no route-removal
        API; this surface already owns every write into that list, so it owns the
        savepoint too."""
        return len(self._app._fast_mcp._additional_http_routes)

    def rollback_module_routes(self, binding: MountBinding, savepoint: int) -> None:
        """Undo every route a failed/quarantined bound module registered, across all
        three surfaces, so ``RouteRegistry.match()``, the cross-owner collision math,
        and the OpenAPI enumeration all see nothing from it: truncate the FastMCP
        route table back to ``savepoint`` and deregister every ``route_registry`` row
        owned by the module.

        Fires for BOTH failure shapes — a ``custom_route`` that raised mid-import
        (undeclared row / explicit ``authed=`` / route-less item) and a post-import
        ``_verify_all_registered`` raise — since both leave the rows committed before
        the fault standing. A misused savepoint (outside the current table) raises."""
        routes = self._app._fast_mcp._additional_http_routes
        if not 0 <= savepoint <= len(routes):
            raise ValueError(f"route-table savepoint {savepoint} is outside the current table of {len(routes)} routes")
        del routes[savepoint:]
        route_registry.rollback_owner(plugin_owner(binding))

    def finalize(self, app):
        """Mount the sub-MCP router and wrap the registered middleware stack.

        ``app`` is the FastMCP-built ASGI app whose lifespan drives the
        streamable-http session-manager task group. Middleware wrappers are plain
        ASGI callables that expose neither ``app``'s ``router`` nor its lifespan,
        so the lifespan-bearing app is recorded on the returned object as
        ``mcp_lifespan_app``. A caller that must enter the lifespan by hand (the
        mounted worker, whose dispatch swallows the lifespan scope) reads it there
        so the FastMCP lifespan is entered regardless of any middleware wrapping.
        """
        lifespan_app = app
        prefix = self._app._mcp_sub_app_router.root_prefix
        app.mount(prefix, self._app._mcp_sub_app_router)
        record_sub_mcp_mount(prefix)

        for mw in self._middlewares.values():
            cls, args, kwargs = mw
            app = cls(app, *args, **kwargs)

        app.mcp_lifespan_app = lifespan_app
        return app


def http_surface() -> HttpSurface:
    """The bound concrete :class:`HttpSurface`, carrying the
    ``declared`` / ``destructive`` metadata seam.

    A native ``/api/*`` handler registers through this so it can declare its
    OpenAPI metadata explicitly (its ``reload_gated`` / ``reads_body`` / error
    statuses / success status), the same way the operation adapter reaches the
    surface. It resolves the concrete surface whichever way the bound app exposes
    it: the offline spec harness binds it as ``tai42_app.http``; the concrete
    server exposes it as ``_http_surface``.
    """
    from tai42_contract.app import tai42_app

    # The concrete server exposes the surface as ``_http_surface``; the offline spec
    # harness binds an app whose ``.http`` IS the ``HttpSurface`` directly. Either
    # way the resolved object is a concrete ``HttpSurface`` carrying the seam.
    surface = getattr(tai42_app, "_http_surface", None)
    if surface is None:
        surface = tai42_app.http
    return cast("HttpSurface", surface)
