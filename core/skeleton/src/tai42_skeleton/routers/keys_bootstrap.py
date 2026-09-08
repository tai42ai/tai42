"""The public first-key bootstrap door — ``POST /api/keys/bootstrap``.

PUBLIC (``authed=False``): a fresh deployment with access control ON has no
authenticated door to mint its first key, so this one is reachable with the gate on and
no key. Runtime public-ness comes from the verifier's declared-public tier reading the
``authed=False`` registration — the route sits outside the reserved ``/api/auth``
namespace, so the tier grants it with no per-deployment route row. The door itself is
token-gated, per-IP throttled, and one-shot (see
:mod:`tai42_skeleton.operations.keys_bootstrap`).
"""

from __future__ import annotations

from json import JSONDecodeError

from pydantic import ValidationError
from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import (
    BadRequestError,
    ValidationRejected,
    operation_metadata_of,
    register_operation_route,
)
from tai42_skeleton.operations.keys_bootstrap import BootstrapKeyBody
from tai42_skeleton.operations.keys_bootstrap import bootstrap_admin_key as _bootstrap_admin_key_op


def _client_ip(request: Request) -> str:
    # The direct peer — no X-Forwarded-For parsing, so the throttle keys on the
    # connection the platform actually sees.
    return request.client.host if request.client else "unknown"


async def _extract_bootstrap(request: Request) -> dict:
    """Validate the body at the HTTP edge and add the caller IP the throttle keys on.

    Owns the parse (so the operation stays request-free yet can read the IP): a malformed
    body is a 400 and a schema-invalid one a 422, before any token or mint work runs.
    """
    try:
        body = await request.json()
    except (JSONDecodeError, ValueError) as exc:
        raise BadRequestError("invalid JSON body") from exc
    try:
        parsed = BootstrapKeyBody.model_validate(body)
    except ValidationError as exc:
        raise ValidationRejected("invalid request body") from exc
    return {
        "user_id": parsed.user_id,
        "description": parsed.description,
        "bootstrap_token": parsed.bootstrap_token,
        "client_ip": _client_ip(request),
    }


bootstrap_admin_key = register_operation_route(
    tai42_app,
    operation_metadata_of(_bootstrap_admin_key_op),
    path="/api/keys/bootstrap",
    method="POST",
    context_extractor=_extract_bootstrap,
    authed=False,
)
