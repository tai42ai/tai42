"""Shared observation hooks for the plugin-distribution reload fixtures.

Records an HTTP route into the live ``route_registry`` singleton (read INDIRECTLY
off the module so a test can swap in a fresh one) and counts each fixture module's
import executions, so a reload test can assert exactly which modules re-fired their
registrations. ``register_route`` resolves the served path through the mount binding
bound for the import — and RAISES if none is bound, modelling a route module that
captures ``mount_base()`` at import time.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from tai42_skeleton.app import route_registry as _route_registry
from tai42_skeleton.app.mount_map import current_mount_binding, note_registered
from tai42_skeleton.app.route_registry import RouteOwner

# Per handler-defining module, how many times its body executed. A reload that
# re-fires a module bumps its count, so a test asserts which modules the reload
# touched — and, crucially, which it left alone.
exec_count: Counter[str] = Counter()


def register_route(
    relative_path: str, method: str, owner: RouteOwner, handler: Callable[[Request], Awaitable[Response]]
) -> None:
    """Record one plugin route under the mount binding bound for this import.

    ``handler`` is defined in the CALLING fixture module, so the registry attributes
    the route to that module (the module a reload must re-fire), and the served path
    resolves through the binding — a reload under the WRONG binding would record a
    different path. Raises off a binding, exactly as a ``mount_base()``-at-import
    module does when it is re-imported bare."""
    binding = current_mount_binding()
    if binding is None:
        raise RuntimeError(f"{handler.__module__} imported with no mount binding bound")
    exec_count[handler.__module__] += 1
    note_registered(relative_path, frozenset({method}))
    _route_registry.route_registry.record(
        path=binding.resolved_path(relative_path),
        methods=[method],
        name=handler.__name__,
        handler=handler,
        summary="fixture route",
        tags=["fixture"],
        authed=False,
        request_model=None,
        response_model=None,
        owner=owner,
        public=True,
    )
