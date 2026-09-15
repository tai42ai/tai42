"""Dispatch one tool turn to its resolved outcome.

The inbound payload maps to the tool kwargs, the tool runs under the bound execution
identity with the generic tool-route completion bound, and the raw result maps to the
reply (or to a silent/error outcome).
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.conversations import AnswerPart, ConversationRoute, Person
from tai42_contract.interactions import (
    LocationElement,
    MediaItem,
    SuspendedInteraction,
    reset_park_completion,
    set_park_completion,
)
from tai42_contract.states.binding import StateBinding
from tai42_contract.tools import ToolInvocation, reset_current_tool_invocation, set_current_tool_invocation
from tai42_kit.utils.data import run_jq_bounded

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.context import _turn_block
from tai42_skeleton.conversations.turn.outcome import (
    _ResolvedOutcome,
    _SilentOutcome,
    _text_part,
    _tool_error,
    _ToolOutcome,
)
from tai42_skeleton.conversations.turn.tool_result import (
    _failed_result_detail,
    _interrupt_result_detail,
    _result_shape,
    _suspended_result_note,
)
from tai42_skeleton.operations.errors import PermissionDeniedError

logger = logging.getLogger("tai42_skeleton.conversations.turn")


def _tool_payload(
    route: ConversationRoute,
    text: str,
    client_address: str,
    thread_id: str,
    *,
    record: ConversationRecord,
    person: Person | None,
    params: dict[str, str] | None,
    form: dict[str, Any] | None,
    attachments: list[MediaItem] | None,
    location: LocationElement | None,
) -> dict[str, object]:
    """Assemble the dispatch payload dict for a tool turn.

    Carries the message/sender/event/person/params/form/attachments/location
    fields plus the generic ``turn`` block. An event turn nulls
    ``message``/``sender`` and carries its structured ``event``; the person and
    the optional structured fields ride only when present.
    """
    payload: dict[str, object] = {
        "message": text,
        "sender": client_address,
        "our_identity": route.our_identity,
        "channel": route.channel,
        "thread_id": thread_id,
        "turn": _turn_block(record, route, person=person, thread_id=thread_id),
    }
    if record.inbound_kind == "event":
        # An event turn has no human text and no sender; its structured payload rides the
        # dedicated ``event`` key, and ``message``/``sender`` are null so a payload_expr
        # (and the default kwargs) never read an event as a text message.
        event = record.inbound_event or {}
        payload["message"] = None
        payload["sender"] = None
        payload["event"] = {"id": event["event_id"], "kind": event["kind"], "payload": event["payload"]}
    if person is not None:
        payload["person_id"] = person.person_id
        payload["person_addresses"] = [a.model_dump(mode="json") for a in person.addresses]
    if params:
        payload["params"] = params
    if form is not None:
        payload["form"] = form
    if attachments is not None:
        payload["attachments"] = [a.model_dump(mode="json") for a in attachments]
    if location is not None:
        payload["location"] = location.model_dump(mode="json")
    return payload


async def _dispatch_tool(
    route: ConversationRoute, kwargs: dict[str, object], thread_id: str, *, route_state_binding: StateBinding | None
) -> object:
    """Run the tool under the bound execution identity and return the raw result.

    The generic tool-route completion is bound around the dispatch and the
    route's optional state binding is set/reset. The caller owns the
    denied/failed dispatch guards.
    """
    async with bind_execution_identity(route.execution_key, bound_fingerprint=route.execution_key_fingerprint):
        # ``completion_delivery`` imports this module, so the constant is read lazily to keep
        # the door↔turn package free of an import cycle.
        from tai42_skeleton.conversations.turn.completion_delivery import DELIVER_TOOL_COMPLETION_NAME

        # Bind the generic tool-route completion for the dispatch: a parking tool captures
        # it, and its resumer fires the deferred outcome back to THIS thread out of band.
        # A non-parking tool never reads it. Reset in a finally so it never leaks past the
        # dispatch.
        # Pin the ORIGINATING route beside the delivery thread: a linked person may write
        # from a different route before the resume, and the completion must map the outcome
        # through THIS route's reply_expr, not the route the thread's newest record names.
        completion_token = set_park_completion(
            DELIVER_TOOL_COMPLETION_NAME,
            {"delivery_thread_id": thread_id, "route_name": route.route_name},
        )
        binding_token = (
            set_current_tool_invocation(ToolInvocation(tool_name=route.target_name, state_binding=route_state_binding))
            if route_state_binding is not None
            else None
        )
        try:
            # ``offload_sync``: a synchronous tool runs off the event loop, matching the
            # meta-executor door, so a blocking tool cannot starve the turn engine.
            return await accessors._tools().run_tool(route.target_name, kwargs, offload_sync=True)
        finally:
            reset_park_completion(completion_token)
            if binding_token is not None:
                reset_current_tool_invocation(binding_token)


async def _tool_outcome_of_result(route: ConversationRoute, result: object) -> _ToolOutcome:
    """Map a raw tool run result to an outcome.

    Applies the ordered SuspendedInteraction / suspended-note / interrupt /
    failure / reply-mapping disposition chain. A paused/suspended run is silent,
    an interrupt or non-success terminal is a client-safe error, and a clean
    result maps through ``reply_expr`` to an answer or (null/blank) a silent
    outcome.
    """
    if isinstance(result, SuspendedInteraction):
        # The tool parked the caller on an async ask (a generic contract sentinel —
        # the turn learns nothing of the driver's resume state): produce no reply and
        # end the turn silently. The completion continuation bound around this dispatch
        # is what delivers the reply back into the thread when the parked run resumes.
        return _SilentOutcome()
    suspended_note = _suspended_result_note(result)
    if suspended_note is not None:
        # The run handed back a NON-TERMINAL SUSPENDED envelope, NOT the SuspendedInteraction
        # marker: an engine-caller async re-park (``"suspended"``) that no tool-face converted to a
        # sentinel. Its flagged reply surface is still DOWNSTREAM of the pause, so mapping it
        # through reply_expr would answer a not-ready reply as a produced one (faulting an authored
        # completeness guard, or delivering a blank). A suspend is NEITHER success nor failure and
        # HAS a delivery leg, so it takes NEITHER the reply path NOR the error path: end the turn
        # silently exactly as the marker branch above does, and the real reply delivers out of band
        # through the completion continuation bound around this dispatch when the resume drives past
        # the pause. The note rides the silent record so it reads as PENDING, not lost.
        logger.info("conversations: tool turn for route %r paused: %s", route.route_name, suspended_note)
        return _SilentOutcome(note=suspended_note)
    interrupt_detail = _interrupt_result_detail(result)
    if interrupt_detail is not None:
        # The run handed back an INTERRUPT envelope. Unlike a suspend, an interrupt pause
        # reaching a conversation turn has NO delivery leg — the turn cannot drive the run, so the
        # reply would never arrive. That is a permanent route misconfiguration, and silencing it
        # would convert a noticed failure into quiet data loss. It is surfaced as the SAME failed
        # turn a non-success terminal is — the route's client-safe error reply to the participant, the
        # cause named in the recorded detail — so the misconfiguration is loud. Logged at WARNING,
        # not info: this is a fault, not a routine pause.
        logger.warning("conversations: tool turn for route %r interrupted: %s", route.route_name, interrupt_detail)
        return _tool_error(interrupt_detail, route)
    failure_detail = _failed_result_detail(result)
    if failure_detail is not None:
        # The tool RETURNED a non-success terminal instead of raising: the run was aborted,
        # stopped early, or failed, so its result is partial by construction. The route carries
        # only a success mapping, so mapping it would answer a half-finished run as a completed
        # one (or fault inside jq). It is surfaced as the SAME failed turn a raising tool is.
        logger.error("conversations: tool turn for route %r failed: %s", route.route_name, failure_detail)
        return _tool_error(failure_detail, route)
    try:
        reply = await _tool_reply(route, result)
    except Exception as exc:
        logger.exception("conversations: mapping the tool result for route %r failed", route.route_name, exc_info=exc)
        # VALUE-FREE ground truth: the mapped envelope's SHAPE — never its participant-content values —
        # so a mapping fault is diagnosed from the structure the run actually returned (which
        # flagged surface is absent vs present-but-empty) instead of inferred from the guard text.
        try:
            logger.exception(
                "conversations: the tool result that failed to map for route %r had shape: %s",
                route.route_name,
                _result_shape(result),
            )
        except Exception:
            logger.exception("conversations: result-shape diagnostic failed")
        return _tool_error(f"reply_expr error: {exc}", route)
    parts = _reply_parts(reply)
    if parts is None:
        return _SilentOutcome()
    return _ResolvedOutcome(answer_status="answered", parts=parts, error=None)


async def _run_tool_turn(
    route: ConversationRoute,
    text: str,
    thread_id: str,
    client_address: str,
    person: Person | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
    *,
    record: ConversationRecord,
) -> _ToolOutcome:
    """Dispatch one tool turn as the route's execution key and return its resolved outcome.

    The outcome is a :class:`_SilentOutcome` or a :class:`_ResolvedOutcome`.
    Stateless per message — no conversation memory. The inbound payload maps to
    the tool kwargs (``payload_expr`` or a fixed ``{message, sender, turn}``), the tool runs under
    the bound execution identity (whose ``run_tool`` seam authorizes the dispatch), and the
    result maps to the reply (``reply_expr`` or a null/string pass-through). The payload
    always carries ``thread_id`` — this turn's canonical thread id, the same opaque string the
    thread doors address (``DELETE /api/conversations/{route_name}/thread?thread_id=``) — so a
    ``payload_expr`` can thread the conversation id through to the tool/flow kwargs. The payload
    carries ``person_id`` and ``person_addresses`` IFF the target has multichannel on;
    ``sender`` stays the sending address either way. Non-empty ``params`` nest under a
    ``params`` key (never merged into the root); ``None``/empty leave the payload unchanged.
    A structured inbound ``form`` (an ask-less form's submission) rides the payload under a
    ``form`` key ONLY when the inbound carried one, so an existing ``payload_expr`` over a
    form-less inbound sees a byte-identical payload; the default no-``payload_expr`` kwargs
    stay the fixed ``{message, sender, turn}`` either way — a route maps the form deliberately or
    not at all. Structured inbound ``attachments`` (the participant's media, as JSON ``MediaItem``
    objects) and a ``location`` (a JSON ``LocationElement``) ride the payload under those stable
    keys under the SAME rule — present ONLY when the inbound carried them, so a media-/location-
    unaware route sees a byte-identical payload and still reads the whole turn as ``message``.
    Every tool turn's payload carries a generic ``turn`` block (:func:`_turn_block`);
    an EVENT turn additionally carries ``event`` and nulls ``message``/``sender``.
    A reply that is ``None`` or blank is a deliberate silent outcome; a mapping fault, a
    denied or failed dispatch, or a wrong-typed result is a client-safe ``error`` outcome
    whose detail is logged, never delivered.

    A result that NAMES a non-success terminal (:func:`_failed_result_detail`) is that SAME
    ``error`` outcome, decided BEFORE any mapping: a tool that reports its failure by returning
    an outcome envelope rather than raising has produced a partial result, and the route's
    ``reply_expr`` maps success only — so mapping it would answer a half-finished run as a
    completed one. The failing status and the envelope's own failure keys ride the recorded
    detail, so the failure stays diagnosable.

    The generic completion continuation (:data:`DELIVER_TOOL_COMPLETION_NAME`) is bound for
    the dispatch's duration, carrying this turn's ``thread_id`` as the opaque delivery
    address. It lets ANY parking tool async-park with a path back to this thread: when the
    parked tool's own resumer drives to a clean terminal out of band, it fires the completion
    with that address so the deferred outcome is mapped through the route's ``reply_expr`` and
    posted back here. A tool that never parks never fires it; the turn stays byte-identical to
    a plain dispatch. The platform learns nothing of the tool's resume machinery — only that a
    park may deliver later through this bound address.
    """
    payload = _tool_payload(
        route,
        text,
        client_address,
        thread_id,
        record=record,
        person=person,
        params=params,
        form=form,
        attachments=attachments,
        location=location,
    )
    try:
        kwargs = await _tool_kwargs(route, payload)
    except Exception as exc:
        # VALUE-FREE: a jq runtime error embeds the offending payload input (which now
        # carries opaque entry params) in its text, and this detail is both logged and
        # persisted into the record — so it names the error CLASS only, never its message
        # and never ``exc_info``. The adjacent tool-run/reply-mapping paths keep their
        # diagnosable text: they render the tool's own output, not the platform's jq input.
        logger.exception(
            "conversations: mapping the inbound payload for route %r failed with %s",
            route.route_name,
            type(exc).__name__,
        )
        return _tool_error(f"payload_expr error ({type(exc).__name__})", route)
    # The route's optional door binding, read from the target config; deposited on the
    # ambient dispatch context so the chokepoint carries it forward and applies it around the
    # tool turn. A route with no binding deposits nothing.
    target_config = await accessors._config_store().get(route.target_kind, route.target_name)
    route_state_binding = target_config.state_binding if target_config is not None else None
    try:
        result = await _dispatch_tool(route, kwargs, thread_id, route_state_binding=route_state_binding)
    except PermissionDeniedError as exc:
        return _tool_error(f"turn denied: {exc}", route)
    except Exception as exc:
        logger.exception("conversations: tool turn for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"turn error: {exc}", route)
    return await _tool_outcome_of_result(route, result)


async def _tool_kwargs(route: ConversationRoute, payload: dict[str, object]) -> dict[str, object]:
    """The kwargs the tool is dispatched with.

    No ``payload_expr`` → the fixed ``{message, sender, turn}`` (plus ``event``
    on an event turn). Otherwise the jq program over the full payload, which MUST
    emit exactly one value and it MUST be a JSON object.
    """
    if route.payload_expr is None:
        kwargs: dict[str, object] = {
            "message": payload["message"],
            "sender": payload["sender"],
            "turn": payload["turn"],
        }
        if "event" in payload:
            kwargs["event"] = payload["event"]
        return kwargs
    # Render the templated payload_expr to its jq program IMMEDIATELY before evaluating it;
    # a by-id text whose stored resource cannot be fetched raises here and is surfaced as
    # the route's loud payload_expr error.
    program = await tai42_app.storage.resource_manager.render_templated_text(route.payload_expr)
    # Bounded at one, so an over-emitting program is capped rather than materialized whole.
    values = await run_jq_bounded(program, payload, 1)
    if len(values) != 1:
        raise ValueError(f"payload_expr must emit exactly one value, emitted {'more than one' if values else 'none'}")
    kwargs = values[0]
    if not isinstance(kwargs, dict):
        raise ValueError(f"payload_expr must emit a JSON object, emitted {type(kwargs).__name__}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    return kwargs


async def _tool_reply(route: ConversationRoute, result: object) -> str | list[AnswerPart] | None:
    """The reply a tool result maps to.

    Either ``None`` for a silent outcome, a single string, or an ORDERED LIST OF
    RICH :class:`AnswerPart` messages the delivery machine sends as separate
    messages in order. No ``reply_expr`` → the result must itself be ``None``, a
    string, or a list. Otherwise the jq program over the raw result, which MUST
    emit exactly one value and it MUST be null, a string, or an array.

    A reply ARRAY is the multi-message authoring surface: each element is EITHER a plain
    string (shorthand for a text-only part) or a part OBJECT (``{message, media?, options?,
    template?}``) — both normalize to the one internal :class:`AnswerPart` model. An empty
    array, a blank string element, or a malformed part object (an unknown key, a bad shape) is
    a loud ``ValueError`` — never silently coerced (order is meaning; parts are strict from
    birth).
    """
    if route.reply_expr is None:
        if result is None or isinstance(result, str):
            return result
        if isinstance(result, list):
            return _checked_reply_parts(result)
        raise ValueError(
            "a tool target with no reply_expr must return null, a string, or a list of parts, "
            f"returned {type(result).__name__}"
        )
    # Render the templated reply_expr to its jq program IMMEDIATELY before evaluating it; a
    # by-id text whose stored resource cannot be fetched raises here and is surfaced as the
    # route's loud reply_expr error.
    program = await tai42_app.storage.resource_manager.render_templated_text(route.reply_expr)
    # Bounded at one, so an over-emitting program is capped rather than materialized whole.
    values = await run_jq_bounded(program, result, 1)
    if len(values) != 1:
        raise ValueError(f"reply_expr must emit exactly one value, emitted {'more than one' if values else 'none'}")
    reply = values[0]
    if reply is None or isinstance(reply, str):
        return reply
    if isinstance(reply, list):
        return _checked_reply_parts(reply)
    raise ValueError(f"reply_expr must emit null, a string, or an array of parts, emitted {type(reply).__name__}")


def _checked_reply_parts(value: list[object]) -> list[AnswerPart]:
    """A tool reply array normalized to ordered :class:`AnswerPart` messages.

    Non-empty, and every element EITHER a plain string (a text-only part) or a
    part object. A blank string, a malformed/unknown-key part object
    (``AnswerPart`` is ``extra="forbid"``), or an unsupported element type is a
    loud ``ValueError`` — an empty array has no message to send, and a bad element
    would deliver an empty or garbled part.
    """
    if not value:
        raise ValueError("a tool reply array must carry at least one message, emitted an empty array")
    parts: list[AnswerPart] = []
    for index, element in enumerate(value):
        if isinstance(element, str):
            if not element.strip():
                raise ValueError(f"tool reply array element {index} is a blank string; a text part must be non-blank")
            parts.append(_text_part(element))
        elif isinstance(element, dict):
            try:
                parts.append(AnswerPart.model_validate(element))
            except ValueError as exc:
                raise ValueError(f"tool reply array element {index} is not a valid part: {exc}") from exc
        else:
            raise ValueError(  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
                f"tool reply array element {index} must be a string or a part object, got {type(element).__name__}"
            )
    return parts


def _reply_parts(reply: str | list[AnswerPart] | None) -> list[AnswerPart] | None:
    """The ordered parts a :func:`_tool_reply` result delivers, or ``None`` for a silent outcome.

    A ``None`` reply and a blank single string are both silent; a non-blank
    single string is one text part; a list is already normalized and validated by
    :func:`_tool_reply`, so it passes through as the ordered parts.
    """
    if reply is None:
        return None
    if isinstance(reply, str):
        return [_text_part(reply)] if reply.strip() else None
    return reply
