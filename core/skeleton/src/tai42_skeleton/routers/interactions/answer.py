"""Human answer and cancel door adapters for the interactions surface."""

from __future__ import annotations

import json
import sys

from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body

from tai42_skeleton.operations import (
    BadRequestError,
    PayloadTooLargeError,
    operation_metadata_of,
    register_operation_route,
)
from tai42_skeleton.operations.interactions import answer_interaction as _answer_interaction_op
from tai42_skeleton.operations.interactions import cancel_interaction as _cancel_interaction_op

from .parse import JSON_PARSE_ERRORS

# This submodule's OWN package object (the one it was imported as part of), captured at
# import time from ``sys.modules`` — NOT ``from tai42_skeleton.routers import interactions``,
# whose parent-attribute read is stale mid-reload. The seam symbols
# (``client_ctx``/``interactions_settings``/…) are read through it at call time, so a test's
# patch on this package alias is the single point the door reads.
_pkg = sys.modules["tai42_skeleton.routers.interactions"]


async def _extract_answer(request: Request) -> dict:
    """Read + bound the human-answer body at the HTTP edge into the operation's flat ``answer`` argument.

    The byte cap (413), invalid JSON (400), and a missing ``answer`` key (400) are the same loud
    rejections the door answers — reproduced here so the operation receives an
    already-parsed answer value (the adapter's plain parse would yield 422).
    """
    settings = _pkg.interactions_settings()
    try:
        raw = await read_bounded_body(request, settings.callback_max_body_bytes)
    except RequestBodyTooLargeError as exc:
        raise PayloadTooLargeError("payload too large") from exc
    request._body = raw
    try:
        body = json.loads(raw)
    except JSON_PARSE_ERRORS as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict) or "answer" not in body:
        raise BadRequestError("body must contain 'answer'")
    return {"answer": body["answer"]}


answer = register_operation_route(
    tai42_app,
    operation_metadata_of(_answer_interaction_op),
    path="/api/interactions/{interaction_id}/answer",
    method="POST",
    context_extractor=_extract_answer,
    action="write",
)


# POST (never DELETE) mirrors the sibling ``/answer`` door: cancel is a named
# state-transition SUB-ACTION on the interaction, not a full-resource delete. The
# router uses no DELETE verb, and DELETE would wrongly connote removing the
# interaction's conversation thread — the one thing this feature explicitly does NOT
# do. The door takes no body (the interaction id is the whole request), so it needs
# no ``context_extractor``: the adapter binds ``interaction_id`` from the path alone.
cancel = register_operation_route(
    tai42_app,
    operation_metadata_of(_cancel_interaction_op),
    path="/api/interactions/{interaction_id}/cancel",
    method="POST",
    action="write",
)
