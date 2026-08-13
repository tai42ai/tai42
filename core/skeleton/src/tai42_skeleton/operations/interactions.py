"""The human answer operation for the ask_user interactions surface.

``answer_interaction`` is the authenticated human answer door
(``POST /api/interactions/{interaction_id}/answer``): the value is validated
server-side against the stored question's ``answer_format`` before the blocked
caller is woken; an invalid answer is rejected loudly and the caller stays
blocked. An EXTERNAL question is answered through its callback URL, never here.

The answer-validation helpers (``_validate_answer``, ``_schema_mismatch``, …),
the reply-TTL clamp, and the serializer-guarded claim live here because the
router's still-handler callback door shares the exact same rules — it imports
them from this module (the store claim, the typed-format validation, the reply
TTL). The router's HTTP-edge extractor reads/parses the request body (the byte
cap → 413, invalid JSON / missing ``answer`` → 400) and hands this operation the
already-parsed ``answer`` value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import jsonschema
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    operation,
)

# The synthetic label recorded when access control is disabled and no caller
# identity exists. A namespaced ``system:`` sentinel (mirroring the
# ``external-callback`` label the callback door records) cannot collide with a
# looked-up user id.
_NO_AUTH_ANSWERED_BY = "system:no-auth"


class _AnswerInvalid(Exception):
    """Raised when a human-door answer fails its stored-format validation.

    ``field`` is the failing answer field's dotted path when the fault is located
    to one (a form schema mismatch), else ``None``; the callback door surfaces it
    as the 400 body's optional ``field`` key so a channel can pin the error on the
    right control."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class InteractionAnswer(BaseModel):
    """An answer to a pending interaction — the ``answer`` value validated at
    runtime against the interaction's own answer schema."""

    answer: Any


def _reply_ttl(request: InteractionRequest) -> int:
    """Short TTL for the reply key ≈ the remaining timeout budget, so a late
    answer to a timed-out question expires instead of resurrecting it."""
    remaining = int((request.timeout_at - datetime.now(UTC)).total_seconds())
    return max(1, remaining)


def _schema_error_field(exc: Exception) -> str | None:
    """The failing ANSWER field's dotted path for a field-located
    ``jsonschema.ValidationError`` (``count``, ``a.b``), or ``None`` when the fault
    has no answer-field location. Only ``jsonschema.ValidationError`` locates a
    fault in the answer: its ``.json_path`` (``$``-rooted, e.g. ``$.count``) names
    the field. ``SchemaError`` also carries a ``.json_path``, but it points INTO the
    stored schema (e.g. ``properties.x.type``) — a location no answering human owns
    — so it is never surfaced; a malformed schema, a root-level ValidationError with
    ``json_path == "$"``, and ``RecursionError`` all yield ``None``."""
    json_path = exc.json_path if isinstance(exc, jsonschema.ValidationError) else None
    if isinstance(json_path, str) and json_path not in ("", "$"):
        # Drop the ``$`` root and a leading ``.`` so a top-level field reads as
        # ``count`` rather than ``$.count``; a nested path keeps its dotted shape.
        return json_path[1:].removeprefix(".")
    return None


def _schema_error_message(exc: Exception) -> str:
    """The 400 message for a schema mismatch, naming the failing ANSWER field (via
    ``_schema_error_field``) when the error locates one so a human on any surface can
    tell WHICH field failed; a pathless fault falls back to the bare message."""
    message = getattr(exc, "message", None) or str(exc)
    field = _schema_error_field(exc)
    if field is not None:
        return f"answer does not match schema at {field}: {message}"
    return f"answer does not match schema: {message}"


# Failures of validating/parsing untrusted input convert to a loud 400; any
# exception outside these sets is a server bug and propagates as a 500.
# RecursionError covers recursive schemas / deeply-nested answers blowing up the
# validator.
_SCHEMA_VALIDATION_ERRORS = (jsonschema.ValidationError, jsonschema.SchemaError, RecursionError)


def _schema_mismatch(answer: Any, schema: dict) -> tuple[str, str | None] | None:
    """Validate ``answer`` against ``schema``; return ``(message, field)`` on a
    validation failure — ``message`` the 400 text, ``field`` the failing answer
    field's dotted path (``None`` for a root-level or otherwise non-locatable fault)
    — or ``None`` when the answer conforms."""
    try:
        jsonschema.validate(answer, schema)
    except _SCHEMA_VALIDATION_ERRORS as exc:
        return _schema_error_message(exc), _schema_error_field(exc)
    return None


def _validate_answer(request: InteractionRequest, answer: Any) -> Any:
    """Validate ``answer`` against the stored format; raise ``_AnswerInvalid``
    (mapped to 400) on mismatch. Returns the validated value."""
    fmt = request.answer_format
    if fmt is AnswerFormat.TEXT:
        if not isinstance(answer, str):
            raise _AnswerInvalid("answer must be a string")
        return answer
    if fmt is AnswerFormat.CONFIRM:
        if not isinstance(answer, bool):
            raise _AnswerInvalid("answer must be a boolean")
        return answer
    if fmt is AnswerFormat.SELECT:
        options = (request.format_payload or {}).get("options", [])
        if answer not in options:
            raise _AnswerInvalid(f"answer must be one of {options}")
        return answer
    if fmt is AnswerFormat.FORM:
        if not isinstance(answer, dict):
            raise _AnswerInvalid("answer must be an object")
        schema = (request.format_payload or {}).get("schema")
        if not isinstance(schema, dict):
            raise _AnswerInvalid("question schema is invalid: missing or non-object schema")
        mismatch = _schema_mismatch(answer, schema)
        if mismatch is not None:
            message, field = mismatch
            raise _AnswerInvalid(message, field=field)
        return answer
    # EXTERNAL is rejected by the answer door before validation runs; any other
    # member reaching here is a server bug, never a client error.
    raise RuntimeError(f"unhandled answer_format: {fmt}")


async def _claim_or_serialization_error(
    store: InteractionStore,
    r: Any,
    response: InteractionResponse,
    group_id: str,
    reply_ttl: int,
    *,
    ticket: str | None = None,
    ticket_ttl: int | None = None,
) -> bool | None:
    """Call ``record_answer``, converting a serializer blowup on an untrusted
    answer into a loud-400 signal. A pathological answer (e.g. a deeply-nested JSON
    object that parsed fine but exceeds the serializer's depth) raises when the
    response is serialized — which happens at the top of ``record_answer`` before
    any Redis write, so catching it here leaves no partial state. Returns the
    claim result (``True``/``False``), or ``None`` to signal "serialization
    failed → answer the caller with a 400"."""
    try:
        return await store.record_answer(r, response, group_id, reply_ttl, ticket=ticket, ticket_ttl=ticket_ttl)
    except (PydanticSerializationError, RecursionError):
        return None


@operation(
    name="answer_interaction",
    summary="Answer a pending interaction",
    tags=["interactions"],
    destructive=True,
    errors=[BadRequestError, ConflictError, ForbiddenError, NotFoundError, PayloadTooLargeError],
    request_model=InteractionAnswer,
)
async def answer_interaction(interaction_id: str, answer: Any) -> dict:
    """Answer a pending interaction through the authenticated human door.

    Audience gate (after the existence/format/status guards): a question's
    ``audience`` identity OR any unrestricted caller (the operator can always unblock
    a stuck question) may answer; every OTHER restricted caller is a loud ``403``.
    An unaddressed question is answerable by any unrestricted caller and by no
    restricted caller. This gate is sound ONLY because a restricted caller can never
    obtain a question's callback ticket — the ticket is delivered exclusively over the
    configured channel (never on any read/stream frame), so the unauthenticated
    callback door stays the sole ticket-bearing surface and no filtered stream leaks
    it."""
    # OFF gate: with no store configured no interaction can exist — a 404
    # byte-identical to the genuine miss below, so the door is no oracle.
    if not interactions_store_configured():
        raise NotFoundError("Interaction not found")
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    user_id, restricted = request_identity()

    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
        if state is None:
            raise NotFoundError("Interaction not found")
        if state.request.answer_format is AnswerFormat.EXTERNAL:
            raise BadRequestError("external interactions are answered via their callback URL")
        if state.status == "answered":
            raise ConflictError("Interaction already answered")
        # A restricted caller may answer ONLY a question addressed to its identity;
        # an unrestricted caller may answer anything.
        if restricted is not None:
            if state.request.audience is None:
                raise ForbiddenError("restricted identities may answer only interactions addressed to them")
            if state.request.audience != restricted:
                raise ForbiddenError("interaction is addressed to another identity")
        try:
            validated = _validate_answer(state.request, answer)
        except _AnswerInvalid as exc:
            raise BadRequestError(str(exc)) from exc
        response = InteractionResponse(
            interaction_id=interaction_id,
            answer=validated,
            # The authenticated caller; with access control off
            # (ACCESS_CONTROL_ENABLE=false) no identity exists, so the reserved
            # no-auth sentinel is recorded.
            answered_by=user_id or _NO_AUTH_ANSWERED_BY,
            answered_at=datetime.now(UTC),
        )
        claimed = await _claim_or_serialization_error(store, r, response, state.group_id, _reply_ttl(state.request))
        if claimed is None:
            raise BadRequestError("answer payload could not be serialized")
        if not claimed:
            raise ConflictError("Interaction already answered")

    return {"interaction_id": interaction_id, "status": "answered"}
