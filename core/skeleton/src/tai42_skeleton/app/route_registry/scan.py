"""Import every router module against a spec harness to enumerate the API route shape.

This is the offline enumeration universe the CLI spec/parity tools and boot audit read.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol, cast

from tai42_skeleton.app.route_registry.metadata import RouteMetadata
from tai42_skeleton.app.route_registry.route_action import (
    _GRANTABLE_ROUTE_ACTIONS,
    _VALID_ROUTE_ACTIONS,
    Handler,
    derive_route_action,
)

# The package object whose attributes carry the overridable seam symbols — the
# ``_import_all_router_modules`` importer and the ``route_registry`` singleton. Read
# THROUGH this object at call time so a ``monkeypatch.setattr`` on the package alias
# bites the reader, and each package instance's submodules bind to their OWN package
# object (correct for the live handlers and the stale handlers a reload leaves behind).
_pkg = sys.modules["tai42_skeleton.app.route_registry"]


class _SpecFastMCP:
    """A no-op stand-in for FastMCP's ``custom_route``, used only for offline metadata capture.

    It returns the handler unchanged, so importing the router modules records
    their metadata without a booted server.
    """

    def custom_route(
        self, path: str, methods: list[str], name: str | None, include_in_schema: bool
    ) -> Callable[[Handler], Handler]:
        return lambda fn: fn


class _SpecLifecycle:
    """A no-op stand-in for the app's ``lifecycle`` seam, used only for offline metadata capture.

    A router module registering a startup/shutdown/reload handler at import time
    gets the handler back unchanged, so no handler is wired and no server is
    needed.
    """

    def on_startup(self, func: Callable[..., object]) -> Callable[..., object]:
        return func

    def on_shutdown(self, func: Callable[..., object]) -> Callable[..., object]:
        return func

    def on_reload(self, func: Callable[..., object]) -> Callable[..., object]:
        return func

    def on_post_swap(self, func: Callable[..., object]) -> Callable[..., object]:
        return func


class _SpecApp:
    """Minimal ``tai42_app`` impl exposing only the ``http`` and ``lifecycle`` seams touched at import.

    The router modules touch only these seams at import time, so metadata
    capture needs no database, Redis, or config.
    """

    def __init__(self) -> None:
        from tai42_skeleton.app.http import HttpSurface

        self._fast_mcp = _SpecFastMCP()
        self.http = HttpSurface(self)  # type: ignore[arg-type]
        self.lifecycle = _SpecLifecycle()

    def effective_router_modules(self) -> None:
        """Signal the whole-package enumeration universe with ``None``.

        No deployment is served under the spec harness, so the universe is the
        whole ``tai42_skeleton.routers`` package, never a curated
        started-manifest set.
        """
        return


class _RouterUniverseSource(Protocol):
    """The one method the shared importer asks of the currently-bound app to choose its universe.

    The forwarding ``tai42_app`` handle is typed as the assembled facade (which
    carries only namespace facets), so this Protocol types the flat method the
    handle forwards to the bound impl — the started ``TaiMCP`` (answers its
    manifest's effective router set) and the offline ``_SpecApp`` (answers
    ``None``) both satisfy it.
    """

    def effective_router_modules(self) -> list[str] | None: ...


def _started_router_modules() -> list[str] | None:
    """The effective router set of a started deployment, or ``None`` when none answers one.

    None covers an unbound process, an offline spec harness, or a partially-faked
    app. The probe must stay a ``hasattr`` on the forwarding handle — a facet
    probe reads a partially-faked app as unbound.
    """
    from tai42_contract.app import tai42_app

    if not hasattr(tai42_app, "effective_router_modules"):
        return None
    return cast("_RouterUniverseSource", tai42_app).effective_router_modules()


def _import_all_router_modules() -> None:
    """Import every module under ``tai42_skeleton.routers``.

    The whole-package enumeration universe, used only offline (CLI spec/parity
    tools, unbooted tests).
    """
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
        _pkg._import_all_router_modules()


def load_api_routes() -> list[RouteMetadata]:
    """The shared route-enumeration primitive: every registered ``/api/*`` route.

    Imports the enumeration universe if needed, then returns its metadata. In a STARTED
    process that universe is the deployment's effective router set (so this enumerates
    exactly the served surface); offline it is the whole router package. Both the OpenAPI
    emitter/coverage gate and the CLI↔route parity gate call this so they enumerate the
    API surface identically.
    """
    _ensure_routers_imported()
    return [meta for meta in _pkg.route_registry.routes() if meta.path.startswith("/api/")]


def load_all_routes() -> list[RouteMetadata]:
    """Return every registered route — the whole self-describing HTTP surface.

    Covers ``/api/*`` and the non-``/api`` operational routes (``/health``,
    ``/ready``, …) alike. Imports the enumeration universe if needed, then
    returns its metadata. In a STARTED
    process that universe is the deployment's effective router set, so this enumerates
    exactly the served surface and never pulls an un-mounted router into the live route
    table; offline it is the whole router package. The access-control resolver derives its
    SPA-shell reserved set from the non-``/api`` GET routes surfaced here, so a route added
    to a router joins the reserved set with no static list to maintain.
    """
    _ensure_routers_imported()
    return _pkg.route_registry.routes()


def route_action_violations() -> list[str]:
    """Return every gated route whose action-class fails the audit — the boot gate's fail list.

    An empty list means clean. A route is an offender when its ``action`` is not
    one of the four valid classes (allow-by-omission is dead) or, for a grantable
    ``read``/``write`` route, when the declared class disagrees with the
    method-derived action. ``fenced``/``secret`` are the explicit admin-only
    fence classes and are exempt from the method-equals-action rule. Public
    (``authed=False``) routes are not gated — their action never enforces — so
    they are not audited here.

    Enumerates through :func:`load_all_routes` so the enumeration universe is imported
    before the audit runs — in a started process that is the deployment's served router
    surface, so the audit judges exactly what the deployment serves; iterating the raw
    registry could pass VACUOUSLY (an empty loop is a silent no-op) had the routers not
    yet been imported.
    """
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
