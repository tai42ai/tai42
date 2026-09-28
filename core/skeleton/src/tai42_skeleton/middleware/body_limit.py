"""App-level request body-size cap — the backstop on every route.

Public doors read their bodies bounded already (a 64 KiB-class cap); the AUTHED
routes read the whole body unbounded (``request.json()`` and friends). This
middleware caps EVERY route in one place: the running total of the ACTUAL body
bytes read is bounded (never a client-declared ``Content-Length``, which is
advisory), and an over-cap request is answered with 413, loudly — never a
silently truncated stream.

The escape signal is a module-private ``_BodyTooLargeError`` that subclasses
``Exception`` DIRECTLY, never ``ValueError``: several routes wrap
``request.json()`` in ``except ValueError`` (``routers/backup.py``,
``routers/_tool_call.py``), so a ``ValueError``-based escape would be swallowed
into a 400 and the 413 would never fire. Raised from the wrapped ``receive`` and
caught around the inner app: before response-start it is converted to a 413,
after start it is re-raised (the stream cannot be un-sent).

This runs INSIDE the base app's own Starlette stack (via the base-app middleware
list, so it sits inside that app's ``ServerErrorMiddleware``): the over-cap escape
must reach this handler and become a 413 before any error middleware turns the
raised ``_BodyTooLargeError`` into a 500. Always on; the app-wide cap is tuned via
``TAI_BODY_LIMIT_MAX_BODY_BYTES``.

A route MAY raise its own bound above the app cap by declaring
``DeclaredRouteMetadata.max_body_bytes`` (a media upload door capped per-kind by the
ingestion seam): the matched route's declared bound drives BOTH the up-front
Content-Length reject and the running-total guard, so that ONE door streams a large
payload while every other route keeps the app cap — never a global raise. The
per-route bounds are compiled into a small table (only the routes that declare one)
memoized against the route registry version, so a route declaring none costs nothing.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tai42_skeleton.access_control.path_canon import strip_root_path
from tai42_skeleton.app.route_registry import DoorTable, RouteMetadata, load_all_routes, route_registry
from tai42_skeleton.settings.body_limit import body_limit_settings


class _BodyTooLargeError(Exception):
    """Internal escape raised once the accumulated body exceeds the cap.

    Subclasses ``Exception`` DIRECTLY (never ``ValueError``) so a route's
    ``except ValueError`` around ``request.json()`` cannot swallow it into a 400.
    """


def _project_body_cap(meta: RouteMetadata) -> int | None:
    """A route's declared per-route body bound, or ``None`` to skip it.

    Routes that declare none are skipped, so the common case is an empty table and the
    per-request lookup is a no-op.
    """
    return meta.max_body_bytes


# The compiled per-route body-bound table, memoized against the registry version that
# produced it: a reload re-records the route surface and bumps that version, so the next
# request rebuilds instead of matching against the previous deployment's declarations.
_body_limit_door_table_obj: DoorTable[int] = DoorTable(
    _project_body_cap,
    load_routes=lambda: load_all_routes(),
    version_of=lambda: route_registry.version,
)


def _body_cap_for(path: str, method: str) -> int | None:
    """The declared per-route body cap covering this request, or ``None`` when no route declares one.

    ``None`` means the app-wide cap applies — the caller falls through to
    ``body_limit_settings().max_body_bytes``.
    """
    door = _body_limit_door_table_obj.door_for(method, path)
    return door.payload if door is not None else None


def _reset_body_limit_door_table_cache() -> None:
    """Drop the memoized table so the next request recompiles it.

    Production invalidation rides the registry version; this is for a test that swaps the
    route surface underneath the middleware without recording into the live registry.
    """
    _body_limit_door_table_obj.reset()


class BodyLimitMiddleware:
    """Caps every request body at ``TAI_BODY_LIMIT_MAX_BODY_BYTES`` actual bytes.

    Over-cap answers 413. Non-http scopes pass straight through.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the inner ASGI ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Cap this request's body, converting an over-cap read into a 413 before response start."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # A route that declares its own bound (a per-kind media upload door) caps at THAT
        # bound; every other route caps at the app-wide default — never a global raise. The
        # door is looked up on the root_path-stripped path — the same canonical path the
        # router and access control match on — so a deployment that carries the mount prefix
        # in ``scope["path"]`` and one whose proxy strips it resolve the same declared bound.
        route_cap = _body_cap_for(strip_root_path(scope["path"], scope.get("root_path", "")), scope["method"].upper())
        per_route = route_cap is not None
        cap = route_cap if route_cap is not None else body_limit_settings().max_body_bytes

        # Up-front reject: a declared Content-Length already over the cap is
        # refused before a single body byte is read.
        if self._declared_length_over_cap(scope, cap):
            await self._reject(cap, per_route, scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    raise _BodyTooLargeError
            return message

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if response_started:
                # The response head is already on the wire; the stream cannot be
                # un-sent, so surface the over-cap loudly rather than truncate.
                raise
            await self._reject(cap, per_route, scope, receive, send)

    @staticmethod
    def _declared_length_over_cap(scope: Scope, cap: int) -> bool:
        """Whether the advisory ``Content-Length`` header already declares a body over the cap.

        The header is advisory, so this only powers the up-front reject; the real bound
        is the running total of body bytes actually read.
        """
        declared = Headers(scope=scope).get("content-length")
        if declared is None:
            return False
        try:
            declared_len = int(declared)
        except ValueError:
            return False
        return declared_len > cap

    @staticmethod
    async def _reject(cap: int, per_route: bool, scope: Scope, receive: Receive, send: Send) -> None:
        detail = (
            f"request body exceeds this route's declared limit ({cap} bytes)"
            if per_route
            else f"request body exceeds TAI_BODY_LIMIT_MAX_BODY_BYTES ({cap} bytes)"
        )
        response = JSONResponse(
            {"error": detail},
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
