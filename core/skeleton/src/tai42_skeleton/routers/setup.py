"""The public setup door — ``POST /api/setup``.

PUBLIC (``authed=False``): a fresh deployment with access control ON has no authenticated
door to initialize itself, so this one is reachable with the gate on and no principal.
Runtime public-ness comes from the verifier's declared-public tier reading the
``authed=False`` registration — the route sits outside the reserved ``/api/auth``
namespace, so the tier grants it with no per-deployment route row. The door itself is
token-gated, per-IP throttled, and one-shot (see :mod:`tai42_skeleton.operations.setup`).
"""

from __future__ import annotations

from json import JSONDecodeError

from pydantic import ValidationError
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.setup import SetupRequest

from tai42_skeleton.operations import (
    BadRequestError,
    ValidationRejectedError,
    operation_metadata_of,
    register_operation_route,
)
from tai42_skeleton.operations.setup import setup_deployment as _setup_deployment_op


def _client_ip(request: Request) -> str:
    # The direct peer — no X-Forwarded-For parsing, so the throttle keys on the
    # connection the platform actually sees.
    return request.client.host if request.client else "unknown"


async def _extract_setup(request: Request) -> dict:
    """Validate the body at the HTTP edge and add the caller IP the throttle keys on.

    Owns the parse (so the operation stays request-free yet can read the IP): a malformed
    body is a 400 and a schema-invalid one a 422, before any token or initialize work runs.
    """
    try:
        body = await request.json()
    except (JSONDecodeError, ValueError) as exc:
        raise BadRequestError("invalid JSON body") from exc
    try:
        parsed = SetupRequest.model_validate(body)
    except ValidationError as exc:
        raise ValidationRejectedError("invalid request body") from exc
    return {
        "setup_token": parsed.setup_token,
        "owner_user_id": parsed.owner_user_id,
        "owner_display_name": parsed.owner_display_name,
        "key_user_id": parsed.key_user_id,
        "key_description": parsed.key_description,
        "login": parsed.login,
        "client_ip": _client_ip(request),
    }


setup_deployment = register_operation_route(
    tai42_app,
    operation_metadata_of(_setup_deployment_op),
    path="/api/setup",
    method="POST",
    context_extractor=_extract_setup,
    authed=False,
)
