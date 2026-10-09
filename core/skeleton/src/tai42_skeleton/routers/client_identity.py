"""``GET /oauth/client-metadata.json`` — the platform's client-identity document (PUBLIC).

An authorization server fetches this URL when a flow presents it as its OAuth ``client_id``. The
route is public by its own declaration (``authed=False``) and acknowledged in the default
``acknowledged_public_routes``; it reads no credential and no request scheme. The origin it
answers for is chosen from the redirect allow-list by the request's ``Host``
(:func:`~tai42_skeleton.connectors.oauth.client_document.origin_for_host`); no matching origin, or
``CONNECTORS_CLIENT_ID_DOCUMENT`` off, is a 404. This module is in the always-on core router tier.
"""

from __future__ import annotations

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse
from tai42_contract.app import tai42_app
from tai42_contract.app.facets.routing import DeclaredRouteMetadata

from tai42_skeleton.connectors.oauth.client_document import DOCUMENT_PATH, client_document, origin_for_host
from tai42_skeleton.connectors.settings import connector_engine_config

# Authorization servers cache the document by its HTTP caching headers.
_CACHE_CONTROL = "max-age=300"


class ClientIdentityDocument(BaseModel):
    """The OAuth client metadata the platform publishes about itself."""

    client_id: str
    client_name: str
    client_uri: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    token_endpoint_auth_method: str


@tai42_app.http.custom_route(
    DOCUMENT_PATH,
    methods=["GET"],
    summary="The platform's OAuth client-identity document",
    tags=["connectors"],
    response_model=ClientIdentityDocument,
    enveloped=False,
    authed=False,
    declared=DeclaredRouteMetadata(reload_gated=False, reads_body=False, error_statuses=(404,), success_status=200),
)
async def client_identity_document(request: Request) -> JSONResponse:
    """Serve the client-identity document for the allow-listed origin the request's ``Host`` names."""
    if not connector_engine_config().client_id_document:
        return JSONResponse({"error": "not found"}, status_code=404)
    origin = origin_for_host(request.headers.get("host"))
    if origin is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = ClientIdentityDocument.model_validate(client_document(origin)).model_dump(mode="json")
    return JSONResponse(body, headers={"Cache-Control": _CACHE_CONTROL})
