"""HTTP routes for the sub-MCP app registry — ``/api/sub-mcp*``.

Three AUTHED doors over the DURABLE sub-MCP registration store, which the live
``tai42_app.sub_app.mcp_sub_app_router`` caches per worker:

* ``GET /api/sub-mcp`` — the registered sub-MCP routes as a JSON-safe view,
  ``{slug: {"tools": [...], "transport": "..."}}``, read from the store so the list
  is coherent across workers (not just this worker's in-process cache).
* ``POST /api/sub-mcp`` — register (or reload) a sub-MCP app exposing ``tools``
  under ``slug`` on an optional ``transport`` (``http`` default). ``slug`` +
  ``tools`` are required; a malformed slug, an unknown tool, or a bad transport is
  rejected before registration (400, or 404 for an unknown tool). The write goes
  through the store-first service, so the registration is durable before the
  in-process router swap.
* ``DELETE /api/sub-mcp/{slug}`` — unregister a sub-MCP app and tear down its
  ASGI lifespan. A slug present in NEITHER the store NOR this worker's router is a
  loud 404; a slug registered on a sibling worker is store-only here and is still
  deletable from any worker.

All three doors are thin adapters over operations in
``tai42_skeleton.operations.sub_mcp`` — no routing/build logic lives here. The POST
body's structural shape (slug pattern, transport, tool-name types) is validated
here at the HTTP edge with typed 400s (producing the operation's flat arguments)
rather than by the adapter's plain request-model parse. Success bodies are
``{"data": ...}``; failures are ``{"error": "<message>"}``.
"""

from __future__ import annotations

from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.adapter import read_json_object
from tai42_skeleton.operations.sub_mcp import list_sub_mcp as _list_sub_mcp_op
from tai42_skeleton.operations.sub_mcp import register_sub_mcp as _register_sub_mcp_op
from tai42_skeleton.operations.sub_mcp import unregister_sub_mcp as _unregister_sub_mcp_op


async def _extract_registration(request: Request) -> dict:
    """Parse + structurally validate the registration body into the operation's flat arguments.

    Yields the ``slug``/``tools``/``transport`` arguments, rejecting a malformed body shape before the
    operation runs (the adapter's plain parse would yield 422; this preserves the explicit 400 surface). The
    slug and transport rules are the operation's, so the projected MCP tool refuses them the same way.
    """
    body = await read_json_object(request)
    slug = body.get("slug")
    tools = body.get("tools")
    if not isinstance(slug, str) or not slug:
        raise BadRequestError("body must contain a non-empty string 'slug'") from None
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise BadRequestError("body must contain a list of tool-name strings 'tools'") from None
    transport = body.get("transport", "http")
    return {"slug": slug, "tools": tools, "transport": transport}


list_sub_mcp = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_sub_mcp_op),
    path="/api/sub-mcp",
    method="GET",
    action="read",
)

register_sub_mcp = register_operation_route(
    tai42_app,
    operation_metadata_of(_register_sub_mcp_op),
    path="/api/sub-mcp",
    method="POST",
    context_extractor=_extract_registration,
    action="write",
)

unregister_sub_mcp = register_operation_route(
    tai42_app,
    operation_metadata_of(_unregister_sub_mcp_op),
    path="/api/sub-mcp/{slug}",
    method="DELETE",
    action="write",
)
