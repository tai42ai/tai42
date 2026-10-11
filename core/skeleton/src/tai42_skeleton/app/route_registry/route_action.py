"""Derive a route's read/write action-class from its declaration + methods.

The authoritative source of a route's authorization character.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from starlette.requests import Request
from starlette.responses import Response

from tai42_skeleton.app.route_shapes import Literal as ShapeLiteral
from tai42_skeleton.app.route_shapes import Shape

Handler = Callable[[Request], Awaitable[Response]]


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
    """Map an HTTP method to its derived action-class — the ONE place read/write is derived.

    ``GET``/``HEAD``/``OPTIONS`` → ``read``; ``POST``/``PUT``/``PATCH``/``DELETE`` → ``write``.

    An unknown/empty method raises loudly (fail-closed) — the derivation never
    defaults to ``read``, so an unclassifiable method is caught at registration/boot
    rather than silently admitted as a read.
    """
    upper = method.upper()
    if upper in _READ_METHODS:
        return "read"
    if upper in _WRITE_METHODS:
        return "write"
    raise ValueError(f"unclassifiable HTTP method {method!r}: cannot derive a read/write action")


def derive_route_action(methods: tuple[str, ...]) -> Literal["read", "write"]:
    """The grantable action-class a route's method set derives to.

    ``write`` when the route serves ANY write method, else ``read``. Enforcement re-derives
    per-method from the live request method (:func:`method_to_action`), so this coarse label
    only classes the route as a whole (the UI grouping and the boot validation).
    """
    return "write" if any(method_to_action(method) == "write" for method in methods) else "read"


# The media type a success body is published under when a route declares none for the method.
JSON_MEDIA_TYPE = "application/json"


def _resolve_route_action(action: RouteAction | None, methods: tuple[str, ...], path: str, authed: bool) -> RouteAction:
    """The route's authoritative action-class.

    Every AUTHED route DECLARES its class explicitly: a grantable ``read``/``write``, or the
    admin-only ``fenced``/``secret`` fence. A declared ``read``/``write`` is VALIDATED against
    every method's derived action, so a misdeclared class (``read`` on a write method) is refused
    at registration. An
    authed route that declares NOTHING BOOT-FAILS here — auto-deriving read/write for an
    undeclared authed route is fail-open (a forgotten fence silently becomes grantable), so
    the omission is refused rather than divined. A PUBLIC route (``authed=False``) never
    enforces an action, so it is exempt and its class is derived from the method for the
    spec — and a ``fenced``/``secret`` class on a public route is itself a contradiction
    (the fence is enforced only in the authenticated path, so a public fence silently opens
    an admin-only door), refused here symmetric with the authed-without-action raise. An
    unknown method raises out of the derivation (fail-closed).
    """
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


def _shape_specificity(shape: Shape) -> int:
    """A shape's specificity for match tie-breaking: its count of LITERAL segments.

    A fixed segment out-ranks any template, so ``/api/x/y`` beats ``/api/x/{z}`` when both match
    a concrete path.
    """
    return sum(1 for segment in shape if isinstance(segment, ShapeLiteral))
