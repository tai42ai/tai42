"""The shared JSON error envelope every adapter route answers with.

:func:`install_error_handlers` installs, on a freshly built base app (``sse_app`` and
``http_app`` alike), the handlers that render an unexpected exception as the generic masked
``500``, a plugin-load failure or a schema-gate refusal of a live rebuild verbatim, a work
refusal during a config reload as the reload gate's retriable ``503``, and the router's native ``404`` as
``{"error": "not found"}``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.errors import ServerReloadingError
from tai42_kit.db import MigrationDiscoveryError, SchemaOutOfDateError

from tai42_skeleton.app.reload_gate import reload_gate

logger = logging.getLogger(__name__)


async def internal_error_handler(request: Request, exc: Exception) -> Response:
    """Uniform 500 for an unexpected application exception on any adapter route.

    Registered as the base app's ``Exception`` handler so ``ServerErrorMiddleware``
    invokes it on BOTH serving paths (``http_app`` / ``sse_app``). It mints a
    correlation ``error_id``, logs the traceback under it, and returns the generic
    envelope — internal exception text (hosts, paths, stack frames) never reaches the
    client. The id is stamped on the exception so the embedding factory's dispatch
    net, which sees the same exception re-raised by ``ServerErrorMiddleware``,
    correlates its own log line and skips a duplicate response.
    """
    error_id = uuid4().hex
    logger.error(
        "unhandled application error [error_id=%s] on %s %s",
        error_id,
        request.method,
        request.url.path,
        exc_info=exc,
    )
    # Attach the id to the arbitrary raised exception so the embedding factory's
    # dispatch net reads it off the re-raised instance. A slotted exception with no
    # ``__dict__`` cannot accept the stamp; the id already rides the response body
    # and the log line, so a failed stamp must never break this last-resort handler.
    with contextlib.suppress(AttributeError):
        exc.error_id = error_id  # pyright: ignore[reportAttributeAccessIssue]
    return JSONResponse({"error": "Internal Server Error", "error_id": error_id}, status_code=500)


async def not_found_handler(request: Request, exc: Exception) -> Response:
    """Render the router's native 404 as the shared JSON error envelope on any adapter route.

    Registered for status ``404`` only, so a known route addressed with the wrong method keeps
    its native ``405``. An unknown path (the SPA catch-all no longer matches an ``/api``/``/mcp``
    one) reaches the router's not-found and is answered ``{"error": "not found"}`` uniformly for
    every method — the same envelope ``_error`` and the operations adapter emit.
    """
    return JSONResponse({"error": "not found"}, status_code=404)


async def core_plugin_boot_error_handler(request: Request, exc: Exception) -> Response:
    """Surface a manifest plugin that could not load during a live rebuild, verbatim.

    A reload door drives an epoch rebuild; a manifest-declared module that is incompatible
    or fails to import, or a loaded plugin that requires a platform feature that is off,
    aborts THAT rebuild, the previous generation keeps serving, and the failure must reach
    the caller instead of the generic masked 500. The message is operator-facing by
    construction — it names what cannot load, why, and the remedy, with no host/path/stack
    detail — so it is surfaced as the shared ``{"error": ...}`` envelope. The status is 500:
    the reload did not apply.
    """
    return JSONResponse({"error": str(exc)}, status_code=500)


async def schema_gate_error_handler(request: Request, exc: Exception) -> Response:
    """Surface a rebuild refused by the schema gate, verbatim.

    A reload door drives an epoch rebuild; the schema gate refuses THAT rebuild before any
    manifest module imports, and the previous generation keeps serving. It refuses a chain the
    deployment depends on that is pending or diverged (``SchemaOutOfDateError``, naming each
    stale chain, its pending files or mismatches, and the ``tai db migrate`` remedy) and a
    chain it cannot resolve (``MigrationDiscoveryError``: a malformed packaged plugin spec
    naming its distribution and file, or a missing or malformed migrations directory). Both
    messages are operator-facing by construction, so they are surfaced as the shared
    ``{"error": ...}`` envelope. The status is 500: the reload did not apply.
    """
    return JSONResponse({"error": str(exc)}, status_code=500)


async def fleet_broadcast_error_handler(request: Request, exc: Exception) -> Response:
    """Surface a fleet-broadcast failure whose cause is an operator-facing rebuild error; mask any other cause.

    A reload that broadcasts to the fleet wraps a failed local rebuild as
    ``FleetBroadcastError``. When the underlying cause is a plugin that could not load
    (``CorePluginBootError``), a route mount collision (``CrossOwnerRouteCollisionError``,
    whose message names the remap remedy) or a schema-gate refusal (``SchemaOutOfDateError`` or
    ``MigrationDiscoveryError``, see :func:`schema_gate_error_handler`), that message plus the fleet report reach the
    caller (the CLI prints it and exits non-zero). Any other cause — a bus transport error
    naming internal hosts — keeps the generic masked 500, so internal detail never leaks.
    """
    from tai42_skeleton.app.route_registry import CrossOwnerRouteCollisionError
    from tai42_skeleton.marketplace.compat import CorePluginBootError
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    cause = exc.__cause__
    operator_facing = (
        CorePluginBootError,
        CrossOwnerRouteCollisionError,
        SchemaOutOfDateError,
        MigrationDiscoveryError,
    )
    if isinstance(exc, FleetBroadcastError) and isinstance(cause, operator_facing):
        return JSONResponse({"error": str(cause), "report": exc.report.model_dump(mode="json")}, status_code=500)
    return await internal_error_handler(request, exc)


async def server_reloading_handler(request: Request, exc: Exception) -> Response:
    """The reload gate's retriable 503 for work refused while a config reload holds the gate."""
    return reload_gate.reject_response()


def install_error_handlers(base_app: Any) -> None:
    """Install the shared error-envelope handlers on a freshly built base app.

    ``FleetBroadcastError`` / ``CorePluginBootError`` / ``SchemaOutOfDateError`` /
    ``MigrationDiscoveryError`` render the operator-facing reload failure;
    ``ServerReloadingError`` (a turn refused while a reload holds the gate, raised from any
    door, a plugin's included) renders the reload gate's retriable 503; every other
    unexpected exception falls back to the generic masked 500, and the router's native 404 is
    rendered as the shared envelope.
    """
    from tai42_skeleton.marketplace.compat import CorePluginBootError
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    base_app.add_exception_handler(Exception, internal_error_handler)
    base_app.add_exception_handler(ServerReloadingError, server_reloading_handler)
    base_app.add_exception_handler(FleetBroadcastError, fleet_broadcast_error_handler)
    base_app.add_exception_handler(CorePluginBootError, core_plugin_boot_error_handler)
    base_app.add_exception_handler(SchemaOutOfDateError, schema_gate_error_handler)
    base_app.add_exception_handler(MigrationDiscoveryError, schema_gate_error_handler)
    base_app.add_exception_handler(404, not_found_handler)
