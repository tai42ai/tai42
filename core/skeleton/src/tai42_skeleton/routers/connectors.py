"""Connectors HTTP surface for the Studio connectors feature.

Routes (all AUTHED), prefixed ``/api/connectors``:

- ``GET    /providers``                        — provider catalog.
- ``GET    /connections``                      — installed connections (no secrets).
- ``GET    /connections/{id}``                 — one connection.
- ``POST   /connections/start``                — begin a Connect (OAuth or no-auth).
- ``DELETE /connections/{id}``                 — disconnect.
- ``POST   /connections/{id}/reconnect``       — re-run the OAuth flow.
- ``PATCH  /connections/{id}/sub-services``    — toggle enabled sub-services.
- ``POST   /tokens/reencrypt``                 — re-encrypt all token and client blobs under the current KEK.
- ``POST   /providers/discover``               — look an MCP server's sign-in up (admin only).
- ``POST   /providers``                        — add an MCP server as a provider (admin only).
- ``DELETE /providers/{id}``                   — remove a provider added by its address (admin only).
- ``PUT    /providers/{id}/client``            — set a provider's client credentials (admin only).
- ``POST   /oauth/complete``                   — finalize a callback (code + signed state).

Every door but the last is a thin adapter over the operations in
``tai42_skeleton.operations.connectors`` — no connector logic lives here. Each
mutating door's body is parsed and validated at the HTTP edge into the operation's
flat arguments (producing an explicit 400 surface), and the
request-derived ``redirect_uri`` / ``origin`` are computed here and handed to the
operation so it stays request-free.

Success bodies are ``{"data": ...}``; failures are ``{"error": "<message>"}`` — except
``/oauth/complete``, whose ``{"data": {...}}`` body carries a ``kind`` discriminator AND
a load-bearing status code (a failed exchange is HTTP 400 with a ``kind`` body). That
shape is not expressible in the ``{"data": ...}`` / ``{"error": ...}`` adapter envelope,
so ``/oauth/complete`` stays a native handler here.

``/oauth/complete`` decodes the HMAC-signed ``state`` envelope via ``oauth.state.decode``:
the single-use ``flow_id`` is recovered from that signed ``state``, and the token
exchange reuses the redirect URI stored in the flow state at authorize-start (so it
stays byte-identical per RFC 6749). Connection views NEVER carry tokens/config
secrets — only the stored, non-secret record fields.
"""

from __future__ import annotations

from json import JSONDecodeError
from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import FanoutSummary
from tai42_contract.connectors.errors import ConnectorClientRequiredError
from tai42_contract.connectors.models import (
    AddServerRequest,
    DiscoverServerRequest,
    PatchSubServicesRequest,
    SetConnectorClientRequest,
    StartConnectRequest,
    StartReconnectRequest,
)
from tai42_kit.db import component_store_configured

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import state
from tai42_skeleton.connectors.oauth.authorization import OAuthAuthorizationCancelledError, OAuthIssuerMismatchError
from tai42_skeleton.connectors.oauth.redirect import compute_deployment_origin, compute_redirect_uri
from tai42_skeleton.connectors.service import connection_service
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.adapter import validation_error_fields
from tai42_skeleton.operations.connector_providers import add_connector_provider as _add_connector_provider_op
from tai42_skeleton.operations.connector_providers import discover_connector_server as _discover_connector_server_op
from tai42_skeleton.operations.connector_providers import remove_connector_provider as _remove_connector_provider_op
from tai42_skeleton.operations.connector_providers import set_connector_client as _set_connector_client_op
from tai42_skeleton.operations.connectors import disconnect as _disconnect_op
from tai42_skeleton.operations.connectors import get_connection as _get_connection_op
from tai42_skeleton.operations.connectors import list_connections as _list_connections_op
from tai42_skeleton.operations.connectors import list_connector_providers as _list_connector_providers_op
from tai42_skeleton.operations.connectors import patch_sub_services as _patch_sub_services_op
from tai42_skeleton.operations.connectors import reconnect as _reconnect_op
from tai42_skeleton.operations.connectors import reencrypt_connector_tokens as _reencrypt_connector_tokens_op
from tai42_skeleton.operations.connectors import start_connect as _start_connect_op


class _BadRequestError(ValueError):
    """A malformed request body — surfaced as a 400."""


class OAuthComplete(BaseModel):
    """The OAuth provider callback payload.

    The signed ``state`` and the authorization ``code``, or a provider-reported ``error``, with the
    redirect's ``iss`` (the issuer that answered, RFC 9207) when it carried one.
    """

    state: str | None = None
    code: str | None = None
    error: str | None = None
    iss: str | None = None


class OAuthNotConfigured(BaseModel):
    """No connector store configured — the flow cannot complete (served 400)."""

    kind: Literal["not_configured"]


class OAuthCancelled(BaseModel):
    """The provider reported the user cancelled or denied the grant (served 200)."""

    kind: Literal["cancelled"]
    message: str


class OAuthFailed(BaseModel):
    """A post-exchange completion failure, served 400.

    ``reason`` is the failure class name (invalid state, alias collision, CAS miss, removed
    provider).
    """

    kind: Literal["failed"]
    reason: str


class OAuthSuccess(BaseModel):
    """A completed connection (served 200).

    The new connection's id, the same-origin ``return_url`` to send the operator back to, and
    the fleet reload fan-out.
    """

    kind: Literal["success"]
    connection_id: str
    return_url: str
    fanout: FanoutSummary


class OAuthCompletionResult(
    RootModel[
        Annotated[
            OAuthNotConfigured | OAuthCancelled | OAuthFailed | OAuthSuccess,
            Field(discriminator="kind"),
        ]
    ]
):
    """The discriminated ``/oauth/complete`` body.

    The door serves ALL four variants through the ``{"data": {"kind": ...}}`` envelope (never the
    ``{"error": ...}`` shape): ``cancelled``/``success`` answer 200, ``not_configured``/``failed``
    answer 400 with the same discriminated body, so the union describes the route's whole success
    surface honestly. The HTTP status is a router behavior, not part of this body model.
    """


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (JSONDecodeError, ValueError) as exc:
        raise _BadRequestError(f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise _BadRequestError("request body must be a JSON object")
    return body


# -- HTTP-edge extractors (validate the body; compute request-derived args) ---


async def _extract_list_connections(request: Request) -> dict:
    """The optional ``?health=`` filter and ``?limit=`` page cap as the list operation's flat arguments.

    A GET reads its parameters from the query string, never a body. ``health`` stays a string the
    operation validates against the health-state enum; ``limit`` is parsed to an int here (a
    non-integer is a loud 400), and the operation range-checks it.
    """
    health = request.query_params.get("health")
    raw_limit = request.query_params.get("limit")
    if raw_limit is None:
        limit: int | None = None
    else:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise BadRequestError(f"limit must be an integer: {raw_limit!r}") from exc
    return {"health": health, "limit": limit}


async def _extract_start_connect(request: Request) -> dict:
    try:
        body = await _json_body(request)
        req = StartConnectRequest.model_validate(body)
    except _BadRequestError as exc:
        raise BadRequestError(str(exc)) from exc
    except ValidationError as exc:
        # Never echo the rejected values (config_values can hold secrets): the body
        # carries only the failing field paths + error types.
        raise BadRequestError("invalid request body", extra={"fields": validation_error_fields(exc)}) from exc
    return {
        "provider_id": req.provider_id,
        "alias": req.alias,
        "enabled_sub_services": req.enabled_sub_services,
        "config_values": req.config_values,
        "return_url": req.return_url,
        "redirect_uri": compute_redirect_uri(request),
        "origin": compute_deployment_origin(request),
    }


async def _extract_reconnect(request: Request) -> dict:
    try:
        body = await _json_body(request)
        req = StartReconnectRequest.model_validate(body)
    except _BadRequestError as exc:
        raise BadRequestError(str(exc)) from exc
    except ValidationError as exc:
        # Field paths + error types only — never the rejected input values.
        raise BadRequestError("invalid request body", extra={"fields": validation_error_fields(exc)}) from exc
    return {
        "enabled_sub_services": req.enabled_sub_services,
        "return_url": req.return_url,
        "redirect_uri": compute_redirect_uri(request),
        "origin": compute_deployment_origin(request),
    }


async def _extract_patch_sub_services(request: Request) -> dict:
    try:
        body = await _json_body(request)
        req = PatchSubServicesRequest.model_validate(body)
    except _BadRequestError as exc:
        raise BadRequestError(str(exc)) from exc
    except ValidationError as exc:
        # Field paths + error types only — never the rejected input values.
        raise BadRequestError("invalid request body", extra={"fields": validation_error_fields(exc)}) from exc
    return {
        "enabled_sub_services": req.enabled_sub_services,
        "return_url": req.return_url,
        "redirect_uri": compute_redirect_uri(request),
        "origin": compute_deployment_origin(request),
    }


def _parse_body[M: BaseModel](request_body: dict, model: type[M]) -> M:
    try:
        return model.model_validate(request_body)
    except ValidationError as exc:
        # Field paths + error types only — never the rejected input values (a body may carry a secret).
        raise BadRequestError("invalid request body", extra={"fields": validation_error_fields(exc)}) from exc


async def _body_of[M: BaseModel](request: Request, model: type[M]) -> M:
    try:
        body = await _json_body(request)
    except _BadRequestError as exc:
        raise BadRequestError(str(exc)) from exc
    return _parse_body(body, model)


async def _extract_discover(request: Request) -> dict:
    req = await _body_of(request, DiscoverServerRequest)
    return {"url": req.url, "provider_id": req.provider_id, "origin": compute_deployment_origin(request)}


async def _extract_add_provider(request: Request) -> dict:
    req = await _body_of(request, AddServerRequest)
    return {
        "url": req.url,
        "provider_id": req.id,
        "display_name": req.display_name,
        "description": req.description,
        "icon_url": req.icon_url,
        "category": req.category,
        "scopes": req.scopes,
    }


async def _extract_set_client(request: Request) -> dict:
    req = await _body_of(request, SetConnectorClientRequest)
    return {
        "provider_id": request.path_params["provider_id"],
        "client_id": req.client_id,
        "client_secret": req.client_secret.get_secret_value() if req.client_secret is not None else None,
        "token_endpoint_auth_method": req.token_endpoint_auth_method,
        "issuer": req.issuer,
    }


# -- Providers / connections / connect flows (operation-backed) --------------


providers = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_connector_providers_op),
    path="/api/connectors/providers",
    method="GET",
    action="read",
)

connections = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_connections_op),
    path="/api/connectors/connections",
    method="GET",
    context_extractor=_extract_list_connections,
    action="read",
)

get_connection = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_connection_op),
    path="/api/connectors/connections/{connection_id}",
    method="GET",
    action="read",
)

start_connect = register_operation_route(
    tai42_app,
    operation_metadata_of(_start_connect_op),
    path="/api/connectors/connections/start",
    method="POST",
    context_extractor=_extract_start_connect,
    action="write",
)

disconnect = register_operation_route(
    tai42_app,
    operation_metadata_of(_disconnect_op),
    path="/api/connectors/connections/{connection_id}",
    method="DELETE",
    action="write",
)

reconnect = register_operation_route(
    tai42_app,
    operation_metadata_of(_reconnect_op),
    path="/api/connectors/connections/{connection_id}/reconnect",
    method="POST",
    context_extractor=_extract_reconnect,
    action="write",
)

patch_sub_services = register_operation_route(
    tai42_app,
    operation_metadata_of(_patch_sub_services_op),
    path="/api/connectors/connections/{connection_id}/sub-services",
    method="PATCH",
    context_extractor=_extract_patch_sub_services,
    action="write",
)

reencrypt_connector_tokens = register_operation_route(
    tai42_app,
    operation_metadata_of(_reencrypt_connector_tokens_op),
    path="/api/connectors/tokens/reencrypt",
    method="POST",
    action="fenced",
)


# -- The provider door (operation-backed; administrators only) ----------------

discover_provider_server = register_operation_route(
    tai42_app,
    operation_metadata_of(_discover_connector_server_op),
    path="/api/connectors/providers/discover",
    method="POST",
    context_extractor=_extract_discover,
    action="fenced",
)

add_provider = register_operation_route(
    tai42_app,
    operation_metadata_of(_add_connector_provider_op),
    path="/api/connectors/providers",
    method="POST",
    context_extractor=_extract_add_provider,
    action="fenced",
)

remove_provider = register_operation_route(
    tai42_app,
    operation_metadata_of(_remove_connector_provider_op),
    path="/api/connectors/providers/{provider_id}",
    method="DELETE",
    action="fenced",
)

set_provider_client = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_connector_client_op),
    path="/api/connectors/providers/{provider_id}/client",
    method="PUT",
    context_extractor=_extract_set_client,
    action="fenced",
)


# -- OAuth completion (native: discriminated body + load-bearing status) ------


def _cancelled() -> JSONResponse:
    return JSONResponse({"data": {"kind": "cancelled", "message": "Sign-in cancelled."}})


def _parse_completion(body: dict) -> tuple[str, str | None, str | None, str | None] | Response:
    """Read ``(flow_id, code, iss, error)`` from the completion body, or the answer that ends the request.

    A provider ``error`` whose ``state`` is missing or does not decode answers "cancelled": there is
    no login to check its issuer against. Without an ``error``, a missing ``state`` or ``code`` is a
    400 and a tampered ``state`` a discriminated ``StateInvalid`` failure.
    """
    provider_error = body.get("error")
    state_param = body.get("state")
    code = body.get("code")
    iss = body.get("iss")
    if iss is not None and not isinstance(iss, str):
        return _error("iss must be a string", 400)
    if not isinstance(state_param, str) or not state_param:
        return _cancelled() if provider_error else _error("state is required", 400)
    if not provider_error and (not isinstance(code, str) or not code):
        return _error("code is required", 400)
    # Recover the single-use flow_id from the HMAC-signed state. A tampered
    # or forged envelope is a loud 400 with a discriminated body.
    try:
        decoded = state.decode(state_param)
    except state.StateInvalidError:
        if provider_error:
            return _cancelled()
        return JSONResponse({"data": {"kind": "failed", "reason": "StateInvalid"}}, status_code=400)
    return (
        decoded.flow_id,
        code if isinstance(code, str) else None,
        iss,
        str(provider_error) if provider_error else None,
    )


@http_surface().custom_route(
    "/api/connectors/oauth/complete",
    methods=["POST"],
    summary="Complete an OAuth connection flow",
    tags=["connectors"],
    request_model=OAuthComplete,
    response_model=OAuthCompletionResult,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=True,
        error_statuses=(400, 401, 500),
        success_status=200,
    ),
    action="write",
)
async def oauth_complete(request: Request) -> Response:
    """Complete an OAuth connection flow, serving the discriminated ``/oauth/complete`` body."""
    # OFF gate: with no connector store configured the flow cannot complete. This
    # door carries a discriminated ``{"data": {"kind": ...}}`` body with a
    # load-bearing status, so the OFF state rides that native envelope — a 400 with
    # ``kind: "not_configured"`` — never the ``{"error": ...}`` shape and never a 501.
    if not component_store_configured(SKELETON_COMPONENT):
        return JSONResponse({"data": {"kind": "not_configured"}}, status_code=400)
    try:
        body = await _json_body(request)
    except _BadRequestError as exc:
        return _error(str(exc), 400)

    parsed = _parse_completion(body)
    if isinstance(parsed, Response):
        return parsed
    flow_id, code, iss, provider_error = parsed
    try:
        # The completion checks the redirect's issuer against the login before it
        # acts on a provider error or the code.
        result = await connection_service.complete_connect(flow_id=flow_id, code=code, iss=iss, error=provider_error)
    except OAuthAuthorizationCancelledError:
        return _cancelled()
    except OAuthIssuerMismatchError:
        # The provider's error fields are neither acted on nor shown.
        return JSONResponse({"data": {"kind": "failed", "reason": "IssuerMismatch"}}, status_code=400)
    except (
        connection_service.AliasInUseError,
        connection_service.ConnectionNotFoundError,
        connection_service.ConcurrentConnectionUpdateError,
        ConnectorClientRequiredError,
        oauth_client.OAuthError,
    ) as exc:
        # A completion failure (alias collision, CAS miss, the connection vanished,
        # the login's stored client removed or refused since the start, or a removed
        # provider surfaced as OAuthError) is a 4xx discriminated failure, never a raw 500.
        return JSONResponse({"data": {"kind": "failed", "reason": type(exc).__name__}}, status_code=400)

    return JSONResponse(
        {
            "data": {
                "kind": "success",
                "connection_id": result.connection_id,
                "return_url": result.return_url,
                "fanout": result.fanout,
            }
        }
    )
