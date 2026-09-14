"""Derive a route's read/write action-class and its success media types from the
handler + methods — the authoritative source of a route's authorization character."""

from __future__ import annotations

import inspect
import re
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


def _shape_specificity(shape: Shape) -> int:
    """A shape's specificity for match tie-breaking: its count of LITERAL segments
    (a fixed segment out-ranks any template), so ``/api/x/y`` beats ``/api/x/{z}``
    when both match a concrete path."""
    return sum(1 for segment in shape if isinstance(segment, ShapeLiteral))
