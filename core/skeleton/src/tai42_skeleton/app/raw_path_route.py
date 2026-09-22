"""A Starlette route that matches on the RAW (undecoded) request path.

The ASGI server percent-decodes the request target once into ``scope["path"]``, so a
``%2F`` inside a path parameter is decoded to ``/`` before the router runs and splits the
parameter across two segments. A parameter whose values legitimately contain ``/`` (a
subject-keyed state record is addressed by ``…/{kind}/{key}`` and a thread ``key`` is
``bridge:{route}:{quote(principal)}/{external_user_id}``) is therefore unroutable: the
single-record doors 404, and a ``key`` whose tail equals a sub-action name
(``…/{key}/writes``) mis-routes to the sub-action door.

:class:`RawPathRoute` matches on ``scope["raw_path"]`` instead, where a client-supplied
``%2F`` is still encoded and so keeps the parameter to ONE segment; every matched
parameter is then percent-decoded (``unquote``) exactly once, so the handler receives the
same decoded value it would for an ordinary segment — the ``key`` with its ``/`` intact.
The access-control layer reasons on the SAME raw target through
``access_control.path_canon.request_canonical_path`` (each segment decoded once, ``/``
re-encoded to ``%2F``), so authz keeps the record ``{key}`` to one segment exactly as this
route does — never a form the router did not match.
"""

from __future__ import annotations

from urllib.parse import unquote

from starlette.routing import Match, Route
from starlette.types import Scope

from tai42_skeleton.access_control.path_canon import strip_root_path, under_prefix


class RawPathRoute(Route):
    """A :class:`~starlette.routing.Route` whose match runs against the raw request path.

    A percent-encoded slash keeps a path parameter to one segment; the matched parameters are decoded
    once after the match.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Match ``scope`` against the raw request path, decoding the matched parameters once."""
        raw_path = scope.get("raw_path")
        if scope["type"] != "http" or raw_path is None:
            # No raw target to reason about (a non-HTTP scope, or an ASGI server that
            # omits raw_path): fall back to the once-decoded match, which is correct for
            # every parameter that carries no encoded slash.
            return super().matches(scope)

        try:
            route_path = _raw_route_path(raw_path, scope.get("root_path", ""))
        except UnicodeDecodeError:
            # A raw target outside ASCII is not a well-formed request path; it matches no
            # raw-path door (access control refuses it as malformed on the same ground).
            return Match.NONE, {}
        match = self.path_regex.match(route_path)
        if not match:
            return Match.NONE, {}

        matched_params = {
            key: unquote(self.param_convertors[key].convert(value)) for key, value in match.groupdict().items()
        }
        path_params = dict(scope.get("path_params", {}))
        path_params.update(matched_params)
        child_scope = {"endpoint": self.endpoint, "path_params": path_params}
        if self.methods and scope["method"] not in self.methods:
            return Match.PARTIAL, child_scope
        return Match.FULL, child_scope


def _raw_route_path(raw_path: bytes, root_path: str) -> str:
    """Return the raw request path as a router-matchable string, with any mounted ``root_path`` stripped.

    Stripped by the same rule access control applies (:func:`strip_root_path`), so both match one form.
    """
    return strip_root_path(raw_path.decode("ascii"), root_path)


class SpaFallbackRoute(Route):
    """The Studio SPA catch-all route, held out of the ``/api`` and ``/mcp`` path spaces.

    The catch-all is registered on ``/{spa_path:path}`` and so path-matches every request,
    ``/api/...`` and ``/mcp/...`` included. Those segments own their own routes; an unknown
    path under them is a genuine 404 the router raises natively, never the SPA shell, and a
    known route addressed with the wrong method keeps its own native 405. :meth:`matches`
    therefore returns :attr:`~starlette.routing.Match.NONE` when the request path's first
    segment is ``api`` or ``mcp`` — the SAME segment-aware exclusion the access-control shell
    tier applies (:func:`~tai42_skeleton.access_control.path_canon.under_prefix`), so the two
    layers cannot drift — and delegates to the ordinary match otherwise.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Never match an ``/api`` or ``/mcp`` path; delegate to the ordinary match otherwise."""
        if scope["type"] == "http":
            # ``strip_root_path`` removes a mounted ``root_path`` exactly as Starlette's own
            # route matching does, so the segment check reads the router-relative path.
            route_path = strip_root_path(scope["path"], scope.get("root_path", ""))
            if under_prefix(route_path, "/api") or under_prefix(route_path, "/mcp"):
                return Match.NONE, {}
        return super().matches(scope)
