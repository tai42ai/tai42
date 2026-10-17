"""The operator's door for registering the reacting-form encryption public key.

``POST /api/channels/whatsapp/flow-encryption`` is authenticated and admin-only (the ``fenced``
route class). It derives the business PUBLIC key from ``CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY`` and
registers it through the Graph API for the sending number: the configured default, or the
``phone_number_id`` the body names. Call it once per sending number, and again after a key
rotation; it keeps no state, so every call derives and registers again.

The private key is read only to derive the public key; it never appears in a reply or a log line.
The public key is returned so the operator can compare it with what Meta shows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_whatsapp.client import set_business_public_key
from tai42_channel_whatsapp.provisioning import flow_encryption_material

logger = logging.getLogger(__name__)


class ProvisionFlowEncryptionBody(BaseModel):
    """The sending number to register the key for; omitted means the configured default."""

    model_config = ConfigDict(extra="forbid")

    phone_number_id: str | None = None


class FlowEncryptionProvisionedResponse(BaseModel):
    """The number the key was registered for and the PEM public key Meta now holds for it."""

    model_config = ConfigDict(extra="forbid")

    phone_number_id: str
    public_key: str


def _ok(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"data": data})


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _body_refusal(exc: ValidationError) -> str:
    """The first field-level reason the body was refused."""
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "body"
    return f"invalid request body: {field}: {first['msg']}"


@tai42_app.http.custom_route(
    "/flow-encryption",
    methods=["POST"],
    summary="Register the business public key for a sending number (reacting-form data endpoint)",
    tags=["channels"],
    request_model=ProvisionFlowEncryptionBody,
    response_model=FlowEncryptionProvisionedResponse,
    action="fenced",
)
async def whatsapp_flow_encryption(request: Request) -> Response:
    """Derive the business public key and register it for the sending number.

    A bad body is 400 (not JSON) or 422 (not this shape). A missing or unreadable private key, or
    no sending number, is 501 naming the setting, with no Graph call. A Graph refusal or transport
    failure (a missing access token included) is 502 carrying the send error.
    """
    raw = await request.body()
    try:
        # ``RecursionError`` is what ``json.loads`` raises on a body nested past the recursion limit.
        parsed: Any = json.loads(raw) if raw else {}
    except (ValueError, RecursionError):
        return _error("invalid JSON body", 400)
    try:
        body = ProvisionFlowEncryptionBody.model_validate(parsed)
    except ValidationError as exc:
        return _error(_body_refusal(exc), 422)

    try:
        material = flow_encryption_material(body.phone_number_id)
    except (ChannelDeliveryError, ValueError) as exc:
        return _error(str(exc), 501)

    try:
        await set_business_public_key(material.phone_number_id, material.public_key_pem)
    except ChannelDeliveryError as exc:
        return _error(str(exc), 502)

    logger.info("registered the Flow encryption public key for phone_number_id %s", material.phone_number_id)
    return _ok({"phone_number_id": material.phone_number_id, "public_key": material.public_key_pem})
