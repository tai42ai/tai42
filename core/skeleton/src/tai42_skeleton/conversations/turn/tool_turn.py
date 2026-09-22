"""Dispatch one tool turn to its resolved outcome.

The inbound payload maps to the tool kwargs, the tool runs under the bound execution
identity with the generic tool-route completion bound, and the raw result maps to the
reply (or to a silent/error outcome).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from tai42_contract.app import tai42_app
from tai42_contract.conversations import AnswerPart, ConversationRoute, Person, TurnSupersededError
from tai42_contract.interactions import (
    LocationElement,
    MediaItem,
    VisitOutcome,
    reset_park_completion,
    set_park_completion,
)
from tai42_contract.states.binding import StateBinding
from tai42_contract.tools import tool_call_frame
from tai42_kit.interactions.door_contract import DOOR_START_DEFAULT, evaluate_door_contract, parked_entries_for_jq
from tai42_kit.utils.data import run_jq_bounded

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.context import _turn_block
from tai42_skeleton.conversations.turn.outcome import (
    _ResolvedOutcome,
    _SilentOutcome,
    _SupersededOutcome,
    _text_part,
    _tool_error,
    _ToolOutcome,
)
from tai42_skeleton.conversations.turn.overlap import is_message_turn
from tai42_skeleton.conversations.turn.tool_result import (
    _failed_result_detail,
    _interrupt_result_detail,
    _result_shape,
    _suspended_result_note,
)
from tai42_skeleton.interactions.visit import list_parked, visit
from tai42_skeleton.operations.errors import PermissionDeniedError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tai42_kit.interactions.door_contract import DoorContractOutcome

    from tai42_skeleton.conversations.turn.overlap import Batch

logger = logging.getLogger("tai42_skeleton.conversations.turn")


def _overlap_message_entries(records: list[ConversationRecord]) -> list[dict[str, object]]:
    """The ``{id, text, accepted_at}`` payload entries for a batch's records, media/form when carried.

    Each record contributes its id, verbatim inbound text and acceptance moment, plus its
    ``form`` / ``attachments`` / ``location`` when it carried them — the shape the ``messages``
    and ``superseded`` payload keys share under ``deliver="all"``.
    """
    entries: list[dict[str, object]] = []
    for r in records:
        entry: dict[str, object] = {"id": r.message_id, "text": r.inbound_text, "accepted_at": r.created_at}
        if r.inbound_form is not None:
            entry["form"] = r.inbound_form
        if r.inbound_attachments is not None:
            entry["attachments"] = [a.model_dump(mode="json") for a in r.inbound_attachments]
        if r.inbound_location is not None:
            entry["location"] = r.inbound_location.model_dump(mode="json")
        entries.append(entry)
    return entries


def _tool_payload(
    route: ConversationRoute,
    text: str,
    client_address: str,
    thread_id: str,
    *,
    record: ConversationRecord,
    batch: Batch,
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
    the optional structured fields ride only when present. Under ``deliver="all"``
    (a message turn only) the payload gains ``messages`` — the batch in order — and,
    when non-empty, ``superseded``; ``message`` is then the turn's whole text and the
    lead's own top-level ``form``/``attachments``/``location`` stay the lead's.
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
        # dedicated ``event`` key, and ``message``/``sender`` are null so a start_expr
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
    if route.overlap.deliver == "all" and is_message_turn(record):
        # The whole turn: the ordered batch, and the superseded records it carries when non-empty.
        # An event turn is never batched, so it never grows these keys.
        payload["messages"] = _overlap_message_entries(batch.members)
        if batch.superseded:
            payload["superseded"] = _overlap_message_entries(batch.superseded)
    return payload


async def _drive_visit(
    route: ConversationRoute,
    thread_id: str,
    *,
    message_id: str,
    contract: DoorContractOutcome,
    kwargs: dict[str, object],
    start_requested: bool,
    route_state_binding: StateBinding | None,
) -> VisitOutcome:
    """Drive the route's parkable run through the shared visit and return its :class:`VisitOutcome`.

    The whole turn runs under the bound execution identity (its ``run_tool`` seam authorizes the
    dispatch and it is the run's fire authority a detached resume re-binds) and the bound generic
    tool-route completion, under which the outermost minting call frame reads that completion as this
    run's out-of-band delivery address. ``visit`` deposits the route's ``state_binding`` around
    ``start`` alone — never around a resume/take/cancel continuation. The caller owns the
    denied/failed dispatch guards.
    """
    async with bind_execution_identity(route.execution_key, bound_fingerprint=route.execution_key_fingerprint):
        # ``completion_delivery`` imports this module, so the constant is read lazily to keep
        # the door↔turn package free of an import cycle.
        from tai42_skeleton.conversations.turn.completion_delivery import DELIVER_TOOL_COMPLETION_NAME

        # Bind the generic tool-route completion for the turn: a parking tool captures it, and its
        # resumer fires the deferred outcome back to THIS thread out of band. A non-parking tool
        # never reads it. Pin the ORIGINATING route beside the delivery thread and this turn's
        # ``message_id`` (the late-path deliverer rebuilds ``$turn`` from that stored record) — a
        # linked person may write from a different route before the resume, and the completion must
        # map the outcome through THIS route's reply_expr, not the route the thread's newest record
        # names. Reset in a finally so it never leaks past the turn.
        completion_token = set_park_completion(
            DELIVER_TOOL_COMPLETION_NAME,
            {"delivery_thread_id": thread_id, "route_name": route.route_name, "message_id": message_id},
        )

        # ``offload_sync``: a synchronous tool runs off the event loop, matching the meta-executor
        # door, so a blocking tool cannot starve the turn engine. ``start`` is ``None`` when the
        # contract asked to start nothing (a ``start_expr`` yielding null).
        async def _start(extras: Mapping[str, Any]) -> object:
            return await accessors._tools().run_tool(route.target_name, kwargs, offload_sync=True, extras=extras)

        try:
            # The outermost minting call frame in its DOOR form (name=None): it mints the run's
            # delivery id and reads the bound completion as its out-of-band address (so a park
            # captures the route's real address, never None) and pushes no chain entry, for a start
            # AND a resume turn alike.
            with tool_call_frame():
                return await visit(
                    target_name=route.target_name,
                    cancel=contract.cancel,
                    resume=contract.resume,
                    start=_start if start_requested else None,
                    extras=contract.extras,
                    state_binding=route_state_binding,
                    receives_outcome=True,
                )
        finally:
            reset_park_completion(completion_token)


async def _tool_outcome_of_result(
    route: ConversationRoute, result: object, *, turn: dict[str, object], parked: list[dict[str, Any]]
) -> _ToolOutcome:
    """Map a started tool run's final result to an outcome.

    Applies the ordered suspended-note / interrupt / failure / reply-mapping disposition chain (a
    caller park or a user park is normalised by the visit, never reaching here). A paused/suspended
    envelope is silent, an interrupt or non-success terminal is a client-safe error, and a clean
    result maps through ``reply_expr`` to an answer or (null/blank) a silent outcome.
    """
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
        reply = await _tool_reply(route, result, turn=turn, asks=[], parked=parked)
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
    batch: Batch,
) -> _ToolOutcome:
    """Dispatch one tool turn as the route's execution key and return its resolved outcome.

    The outcome is a :class:`_SilentOutcome` or a :class:`_ResolvedOutcome`.
    Stateless per message — no conversation memory. The inbound payload maps to
    the tool kwargs (``start_expr`` or a fixed ``{message, sender, turn}``), the tool runs under
    the bound execution identity (whose ``run_tool`` seam authorizes the dispatch), and the
    result maps to the reply (``reply_expr`` or a null/string pass-through). The payload
    always carries ``thread_id`` — this turn's canonical thread id, the same opaque string the
    thread doors address (``DELETE /api/conversations/{route_name}/thread?thread_id=``) — so a
    ``start_expr`` can thread the conversation id through to the tool/flow kwargs. The payload
    carries ``person_id`` and ``person_addresses`` IFF the target has multichannel on;
    ``sender`` stays the sending address either way. Non-empty ``params`` nest under a
    ``params`` key (never merged into the root); ``None``/empty leave the payload unchanged.
    A structured inbound ``form`` (an ask-less form's submission) rides the payload under a
    ``form`` key ONLY when the inbound carried one, so an existing ``start_expr`` over a
    form-less inbound sees a byte-identical payload; the default no-``start_expr`` kwargs
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
        batch=batch,
        person=person,
        params=params,
        form=form,
        attachments=attachments,
        location=location,
    )
    # The run's currently parked interactions on this turn's subject, read once and bound as
    # ``$parked`` in every door jq, dumped to the one compact shape every door presents.
    parked = parked_entries_for_jq(await list_parked())
    try:
        contract = await evaluate_door_contract(route, payload, parked)
    except Exception as exc:
        # VALUE-FREE: a jq runtime error embeds the offending payload input (which now
        # carries opaque entry params) in its text, and this detail is both logged and
        # persisted into the record — so it names the error CLASS only, never its message
        # and never ``exc_info``. The adjacent tool-run/reply-mapping paths keep their
        # diagnosable text: they render the tool's own output, not the platform's jq input.
        logger.exception(
            "conversations: evaluating the door contract for route %r failed with %s",
            route.route_name,
            type(exc).__name__,
        )
        return _tool_error(f"door contract error ({type(exc).__name__})", route)
    kwargs, start_requested = _start_kwargs(payload, contract.start)
    # The route's optional door binding, read from the target config; handed to ``visit``, which
    # deposits it around ``start`` alone. A route with no binding deposits nothing.
    target_config = await accessors._config_store().get(route.target_kind, route.target_name)
    route_state_binding = target_config.state_binding if target_config is not None else None
    try:
        outcome = await _drive_visit(
            route,
            thread_id,
            message_id=record.message_id,
            contract=contract,
            kwargs=kwargs,
            start_requested=start_requested,
            route_state_binding=route_state_binding,
        )
    except TurnSupersededError as exc:
        # The tool read the pending seam and yielded to a newer message: resolve the turn
        # ``superseded`` exactly as the cancel watcher does — no reply, no error reply, no
        # delivery. Caught BEFORE the generic arm so a yield is never mistaken for a turn error.
        return _SupersededOutcome(exc.successor_id)
    except PermissionDeniedError as exc:
        return _tool_error(f"turn denied: {exc}", route)
    except Exception as exc:
        logger.exception("conversations: tool turn for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"turn error: {exc}", route)
    return await _outcome_of_visit(route, outcome, turn=cast("dict[str, object]", payload["turn"]), parked=parked)


def _default_tool_kwargs(payload: dict[str, object]) -> dict[str, object]:
    """The fixed ``{message, sender, turn}`` (plus ``event``) a route with no ``start_expr`` dispatches."""
    kwargs: dict[str, object] = {
        "message": payload["message"],
        "sender": payload["sender"],
        "turn": payload["turn"],
    }
    if "event" in payload:
        kwargs["event"] = payload["event"]
    return kwargs


def _start_kwargs(payload: dict[str, object], start: object) -> tuple[dict[str, object], bool]:
    """The tool kwargs to dispatch and whether a start runs at all.

    :data:`DOOR_START_DEFAULT` (no ``start_expr``) → the fixed default kwargs, start runs;
    ``None`` (``start_expr`` yielded null) → start nothing; a dict → those kwargs, start runs.
    """
    if start is DOOR_START_DEFAULT:
        return _default_tool_kwargs(payload), True
    if start is None:
        return {}, False
    return cast("dict[str, object]", start), True


async def _outcome_of_visit(
    route: ConversationRoute, outcome: VisitOutcome, *, turn: dict[str, object], parked: list[dict[str, Any]]
) -> _ToolOutcome:
    """Map a :class:`VisitOutcome` to the turn's outcome.

    ``result`` → the started run's final result mapped through the disposition chain; ``asks`` → the
    caller asks mapped through ``reply_expr`` (``.`` = null, ``$asks`` = the entries) or silent with
    no ``reply_expr``; ``parked`` (only user asks left) and ``none`` (nothing ran) → silent, the
    reply delivering out of band through the bound completion when a parked run resumes.
    """
    if outcome.kind == "result":
        return await _tool_outcome_of_result(route, outcome.result, turn=turn, parked=parked)
    if outcome.kind == "asks":
        asks = parked_entries_for_jq(outcome.asks)
        try:
            reply = await _tool_reply(route, None, turn=turn, asks=asks, parked=parked)
        except Exception as exc:
            logger.exception(
                "conversations: mapping the caller asks for route %r failed", route.route_name, exc_info=exc
            )
            return _tool_error(f"reply_expr error: {exc}", route)
        parts = _reply_parts(reply)
        if parts is None:
            return _SilentOutcome()
        return _ResolvedOutcome(answer_status="answered", parts=parts, error=None)
    return _SilentOutcome()


async def _tool_reply(
    route: ConversationRoute,
    result: object,
    *,
    turn: dict[str, object] | None,
    asks: list[dict[str, Any]],
    parked: list[dict[str, Any]],
) -> str | list[AnswerPart] | None:
    """The reply a started run's outcome maps to.

    Either ``None`` for a silent outcome, a single string, or an ORDERED LIST OF
    RICH :class:`AnswerPart` messages the delivery machine sends as separate
    messages in order. No ``reply_expr`` → the result must itself be ``None``, a
    string, or a list (so a caller-asks outcome, whose ``result`` is null, is silent).
    Otherwise the jq program over the run's result as ``.`` (null for a caller-asks outcome),
    with ``$turn`` (this turn's ids/subject, null on a late delivery whose record aged out),
    ``$asks`` (the caller ask entries, empty on a plain result) and ``$parked`` (the run's parked
    interactions) bound beside it; it MUST emit exactly one value and it MUST be null, a string,
    or an array.

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
    # Bounded at one, so an over-emitting program is capped rather than materialized whole. The
    # result is the ``.`` (null when the run asked its caller); ``$turn`` / ``$asks`` / ``$parked``
    # ride beside it (``$turn`` null on a late delivery whose originating record has aged out).
    values = await run_jq_bounded(program, result, 1, variables={"turn": turn, "asks": asks, "parked": parked})
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
