"""``GET /.well-known/oauth-protected-resource/{endpoint_path}`` — MCP endpoint metadata documents (PUBLIC).

One RFC 9728 metadata document per MCP endpoint, read by an MCP client before it signs in:
the endpoint's public URL (``resource``), the authorization servers it accepts tokens from,
and how a token is presented. Served only while the door is on (an active identity provider
declares an authorization server and ``ACCESS_CONTROL_PUBLIC_BASE_URL`` is set) and only for
the path of an MCP endpoint itself; every other request is a 404. The answer is the same for
every well-formed sub-MCP slug, registered or not, so the document reveals no slug.

Public by its own ``authed=False`` declaration and acknowledged by its template in
``ACCESS_CONTROL_ACKNOWLEDGED_PUBLIC_ROUTES``; it reads no credential. Mounted in the
always-on core router tier, so a pointer never names a document a lean deployment does not
serve.
"""

from __future__ import annotations

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control.advertised import (
    PROTECTED_RESOURCE_METADATA_PREFIX,
    advertised_authorization_servers,
    mcp_resource_url,
)
from tai42_skeleton.access_control.path_canon import MalformedPathError, canonicalize_path
from tai42_skeleton.app.mcp_endpoints import mcp_endpoint_for


class ProtectedResourceMetadata(BaseModel):
    """The RFC 9728 protected-resource metadata of one MCP endpoint."""

    resource: str
    authorization_servers: list[str]
    bearer_methods_supported: list[str]


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not found"}, status_code=404)


@tai42_app.http.custom_route(
    f"{PROTECTED_RESOURCE_METADATA_PREFIX}/{{endpoint_path:path}}",
    methods=["GET"],
    summary="MCP endpoint protected-resource metadata",
    tags=["mcp"],
    response_model=ProtectedResourceMetadata,
    enveloped=False,
    authed=False,
)
async def protected_resource_metadata(request: Request) -> JSONResponse:
    """Answer the protected-resource metadata of the MCP endpoint at ``endpoint_path``, or 404."""
    servers = advertised_authorization_servers()
    if not servers:
        return _not_found()
    try:
        endpoint = canonicalize_path(f"/{request.path_params['endpoint_path']}")
    except MalformedPathError:
        return _not_found()
    if mcp_endpoint_for(endpoint) != endpoint:
        return _not_found()
    body = ProtectedResourceMetadata(
        resource=mcp_resource_url(endpoint),
        authorization_servers=list(servers),
        bearer_methods_supported=["header"],
    )
    return JSONResponse(body.model_dump(), headers={"Cache-Control": "no-store"})
