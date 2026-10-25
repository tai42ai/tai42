"""Sub-MCP app registry operations — list, register/reload, and unregister.

Three operations over the DURABLE sub-MCP registration store, which the live
``tai42_app.sub_app.mcp_sub_app_router`` caches per worker:

* ``list_sub_mcp`` reads the durable store (not this worker's in-process cache) so
  the listing is coherent across workers.
* ``register_sub_mcp`` registers (or reloads) a sub-MCP app exposing ``tools``
  under ``slug`` on an optional ``transport``. The write goes through the
  store-first service, so the registration is durable before the in-process router
  swap. A tool the live registry does not serve is a loud 400 naming each such tool;
  the structural body validation (slug shape, transport, tool-name types) is a loud
  400 raised by the route's extractor.
* ``unregister_sub_mcp`` unregisters a sub-MCP app and tears down its ASGI
  lifespan; a slug present in NEITHER the store NOR this worker's router is a loud
  404, but a slug registered on a sibling worker is store-only here and is still
  deletable from any worker.

Both mutating operations are reload-gated (they rebind live tools) — the adapter
holds the retriable 503 on the route edge while a reload owns the gate.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tai42_skeleton.app.sub_mcp_app import (
    assert_served_tools_resolve,
    sub_mcp_access_pattern,
    sub_mcp_mount_url,
    validate_registration,
)
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.response_models_group_c import (
    SubMcpMapListing,
    SubMcpRegistrationResult,
    SubMcpRemovalResult,
)
from tai42_skeleton.sub_mcp import service
from tai42_skeleton.sub_mcp.store import get_sub_mcp_store


class SubMcpRegistration(BaseModel):
    """A sub-MCP app registration.

    The ``slug`` to mount it under, the tool names it exposes, and the optional
    ``transport`` (``http`` default).
    """

    slug: str = Field(min_length=1)
    tools: list[str]
    transport: str = "http"


@operation(summary="List the registered sub-MCP apps", tags=["sub-mcp"], response_model=SubMcpMapListing)
async def list_sub_mcp() -> dict:
    """List the registered sub-MCP apps from the durable store, keyed by slug, with each served mount."""
    # Read the durable store, not this worker's in-process cache, so the list is
    # coherent across workers. RouteConfig is a pydantic model; model_dump yields
    # the JSON-safe fields (tools + transport) so no live object leaks into the body.
    routes = await get_sub_mcp_store().list_routes()
    return {
        slug: {
            **config.model_dump(),
            "mount_url": sub_mcp_mount_url(slug),
            "access_pattern": sub_mcp_access_pattern(slug),
        }
        for slug, config in routes.items()
    }


@operation(
    summary="Register or reload a sub-MCP app",
    tags=["sub-mcp"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=SubMcpRegistration,
    response_model=SubMcpRegistrationResult,
)
async def register_sub_mcp(slug: str, tools: list[str], transport: str = "http") -> dict:
    """Register or reload the sub-MCP app ``slug`` exposing ``tools`` on ``transport``; an unserved tool is a 400."""
    # The slug and transport rules are the core's, checked first so every door (the HTTP
    # route and the projected MCP tool) refuses them with the same 400.
    try:
        validate_registration(slug, transport)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    # Resolve every tool BEFORE the store write: an unserved name would otherwise fail
    # the FIRST request to the app, a 500 to a different caller.
    await assert_served_tools_resolve(tools)
    # Store write FIRST (durable), then the in-process router swap — the service
    # owns that ordering so a registration survives a crash between the two, and
    # announces the mount change to its listeners.
    await service.register_sub_mcp_app(slug, tools, transport=transport)
    return {"slug": slug, "tools": tools, "transport": transport}


@operation(
    summary="Unregister a sub-MCP app",
    tags=["sub-mcp"],
    reload_gated=True,
    errors=[NotFoundError],
    response_model=SubMcpRemovalResult,
)
async def unregister_sub_mcp(slug: str) -> dict:
    """Unregister the sub-MCP app ``slug`` and tear down its lifespan; an unknown slug is a 404."""
    # The service deletes from the store and, if bound here, the local router. It
    # returns whether anything was removed (store OR local): a slug present in
    # neither is a loud 404, but a slug registered on a sibling worker is store-only
    # here and is still deletable from any worker.
    removed = await service.unregister_sub_mcp_app(slug)
    if not removed:
        raise NotFoundError(f"sub-MCP app {slug!r} not found")
    return {"slug": slug, "removed": True}
