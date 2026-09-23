"""Run one agent turn to its resolved outcome, as the route's execution key."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from tai42_contract.agent.events import (
    InterruptFinal,
    MessageFinal,
    RecursionLimitFinal,
    StructuredFinal,
    StructuredOutputUnresolvedFinal,
    SuspendedFinal,
    final_event_for_value,
)
from tai42_contract.conversations import ConversationRoute, Person
from tai42_contract.interactions import (
    LocationElement,
    MediaItem,
    SuspendedInteraction,
    reset_park_completion,
    set_park_completion,
)
from tai42_contract.template import TemplatedText
from tai42_contract.tools import tool_call_frame
from tai42_kit.interactions.door_contract import DOOR_START_DEFAULT, evaluate_door_contract, parked_entries_for_jq

from tai42_skeleton.authz.execution import authorize_execution_agent_run, bind_execution_identity
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.outcome import (
    _ResolvedOutcome,
    _serialize_structured,
    _SilentOutcome,
    _text_part,
    _tool_error,
    _ToolOutcome,
)
from tai42_skeleton.conversations.turn.tool_turn import _reply_parts, _tool_payload, _tool_reply
from tai42_skeleton.conversations.turn_context import BridgeTurnContext, bridge_turn_context
from tai42_skeleton.interactions.visit import list_parked, visit
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.tools.turn_budget import drive_live_caller_astream

if TYPE_CHECKING:
    from tai42_contract.agent import Agent
    from tai42_contract.interactions import VisitOutcome

    from tai42_skeleton.conversations.models import ConversationRecord
    from tai42_skeleton.conversations.turn.overlap import Batch

logger = logging.getLogger("tai42_skeleton.conversations.turn")

# The astream kwargs the route (never a ``start_expr``) owns: the run's thread and its checkpoint
# continuity. A ``start_expr`` that emits one of these would fight the route for the run's identity,
# so it is refused loudly before the drive rather than silently overriding it.
_ROUTE_OWNED_AGENT_KEYS = frozenset({"thread_id", "resume", "resume_checkpoint_id", "checkpoint_provider"})


def _agent_run_kwargs(text: str, start: Any) -> tuple[dict[str, Any], bool]:
    """The ``astream`` kwargs to drive with and whether a start runs at all.

    :data:`DOOR_START_DEFAULT` (no ``start_expr``) → the default ``{user_message}`` from the rendered
    turn text, start runs; ``None`` (``start_expr`` yielded null) → start nothing; a dict → those
    kwargs, start runs, after refusing any route-owned key. The route supplies ``thread_id``
    separately, so a ``start_expr`` naming it (or a checkpoint key) is refused.
    """
    if start is DOOR_START_DEFAULT:
        return {"user_message": TemplatedText(content=text)}, True
    if start is None:
        return {}, False
    owned = sorted(_ROUTE_OWNED_AGENT_KEYS & set(start))
    if owned:
        raise ValueError(f"start_expr for an agent target must not set the route-owned key(s) {owned}")
    return dict(start), True


def _has_pending_caller_ask(parked: list[dict[str, Any]]) -> bool:
    """Whether a caller ask is still ``asking`` (or its resume still ``running``) on the run's subject.

    A fresh agent start while one stands would run a second turn against a thread whose caller is
    still owed an answer, so the start is refused.
    """
    return any(entry.get("to") == "caller" and entry.get("status") in ("asking", "running") for entry in parked)


async def _agent_suspended(final: SuspendedFinal) -> SuspendedInteraction:
    """The :class:`SuspendedInteraction` a park's :class:`SuspendedFinal` normalises to for ``visit``.

    The caller/user split ``visit`` classifies on is read off the run's own parked list (read FRESH
    after the park, so the newly-parked asks are seen): the ids parked ``to="caller"`` are the ones
    the reply maps through ``reply_expr`` (``asks``); the rest are user asks delivered out of band
    (``parked``).
    """
    parked = await list_parked()
    ids = set(final.interaction_ids)
    caller_ids = [entry.id for entry in parked if entry.to == "caller" and entry.id in ids]
    return SuspendedInteraction(
        interaction_id=final.interaction_ids[0],
        interaction_ids=list(final.interaction_ids),
        caller_interaction_ids=caller_ids,
    )


async def _drive_agent_terminal(agent: Agent, run_kwargs: dict[str, Any]) -> Any:
    """Drive the agent run to its terminal and return the value ``visit`` normalises.

    A park (:class:`SuspendedFinal`) → a :class:`SuspendedInteraction`; a finish
    (:class:`StructuredFinal` / :class:`MessageFinal`) → the final event itself (``visit`` classifies
    it a ``result``). An :class:`InterruptFinal` is not answerable by a background turn, so it is
    raised. Routed through the shared live-caller seam so the turn is budgeted and its trace
    attributed.
    """
    async for event in drive_live_caller_astream(agent.astream(**run_kwargs)):
        if isinstance(event, SuspendedFinal):
            return await _agent_suspended(event)
        if isinstance(event, InterruptFinal):
            raise RuntimeError(f"agent raised an interrupt ({event.interrupt_id}) a background turn cannot answer")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
        if isinstance(event, StructuredFinal | MessageFinal | StructuredOutputUnresolvedFinal | RecursionLimitFinal):
            return event
    return None


def _final_event(result: Any) -> StructuredFinal | MessageFinal:
    """A finished agent run's terminal as a final event, whichever path produced it.

    The start drive yields the final event itself; a resume returns the run's plain terminal value,
    which the platform's value rule maps to the SAME final event (a ``str`` → a message final, any
    other value → a structured final). A typed non-fatal outcome (structured-output re-prompt cap
    reached, or the recursion limit hit) — arriving as the event from the start drive or as the same
    model from a resumed ``run`` — surfaces as a :class:`StructuredFinal` carrying the outcome's own
    fields as data, so ``reply_expr`` can branch on the typed outcome rather than a generic failure.
    Both paths reach :func:`_agent_result_outcome` as one shape.
    """
    if isinstance(result, StructuredOutputUnresolvedFinal | RecursionLimitFinal):
        return StructuredFinal(data=result.model_dump(mode="json"))
    if isinstance(result, StructuredFinal | MessageFinal):
        return result
    return final_event_for_value(result)


async def _agent_result_outcome(
    route: ConversationRoute,
    final: StructuredFinal | MessageFinal,
    *,
    turn: dict[str, object],
    parked: list[dict[str, Any]],
) -> _ToolOutcome:
    """Map a finished agent run's terminal to an outcome.

    With no ``reply_expr`` the structured final is serialized to one string (a message final passes
    its text); an empty answer is the SAME client-safe error the fresh-turn path gives. With a
    ``reply_expr`` set the structured data (a message final's text) is the ``.`` mapped through it,
    skipping the string serialization, and a null/blank reply is a designed silent outcome.
    """
    structured = final.data if isinstance(final, StructuredFinal) else final.text
    if route.reply_expr is None:
        text = _serialize_structured(structured) if isinstance(final, StructuredFinal) else final.text
        if not text.strip():
            return _tool_error("agent produced an empty answer", route)
        return _ResolvedOutcome(answer_status="answered", parts=[_text_part(text)], error=None)
    reply = await _tool_reply(route, structured, turn=turn, asks=[], parked=parked)
    parts = _reply_parts(reply)
    if parts is None:
        return _SilentOutcome()
    return _ResolvedOutcome(answer_status="answered", parts=parts, error=None)


async def _agent_asks_outcome(
    route: ConversationRoute, asks: list[dict[str, Any]], *, turn: dict[str, object], parked: list[dict[str, Any]]
) -> _ToolOutcome:
    """Map a run that asked its caller through ``reply_expr`` (``.`` null, ``$asks`` the entries), or silent."""
    reply = await _tool_reply(route, None, turn=turn, asks=asks, parked=parked)
    parts = _reply_parts(reply)
    if parts is None:
        return _SilentOutcome()
    return _ResolvedOutcome(answer_status="answered", parts=parts, error=None)


async def _agent_outcome_of_visit(
    route: ConversationRoute, outcome: VisitOutcome, *, turn: dict[str, object], parked: list[dict[str, Any]]
) -> _ToolOutcome:
    """Map an agent run's :class:`VisitOutcome` to the turn's outcome.

    ``result`` → the finished run mapped through ``reply_expr`` (or serialized); ``asks`` → the caller
    asks mapped through ``reply_expr`` or silent; ``parked`` (a user ask) → silent, its answer
    delivered out of band; ``none`` (the drive ended with no terminal event, a malformed empty run) →
    the SAME client-safe error the fresh-turn path gives for an empty answer, never a silent drop.
    """
    if outcome.kind == "result":
        return await _agent_result_outcome(route, _final_event(outcome.result), turn=turn, parked=parked)
    if outcome.kind == "asks":
        asks = parked_entries_for_jq(outcome.asks)
        return await _agent_asks_outcome(route, asks, turn=turn, parked=parked)
    if outcome.kind == "parked":
        return _SilentOutcome()
    return _tool_error("agent produced an empty answer", route)


async def _run_agent_turn(
    route: ConversationRoute,
    text: str,
    thread_id: str,
    client_address: str,
    *,
    record: ConversationRecord,
    batch: Batch,
    person: Person | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> _ToolOutcome:
    """Run one agent turn as the route's execution key, through the shared visit, and return its outcome.

    The identity is bound for the turn's duration and the run authorized against it before the agent
    runs. The route's door contract (``cancel_expr`` / ``resume_expr`` / ``start_expr`` /
    ``extras_expr``) is evaluated over the turn payload with the run's parked interactions bound as
    ``$parked``; ``start_expr`` builds the ``astream`` kwargs (default ``{user_message}``), the
    cancel/resume jqs act on the parked interactions, and the terminal maps to a reply. A denied run,
    a mid-turn error, an interrupt or an empty answer becomes a client-safe ``error`` outcome; a run
    that parks a USER ask is a silent outcome, its resumed answer delivered out of band through the
    bound completion continuation; a run that asks its CALLER maps through ``reply_expr``.

    The turn-scoped bridge context is established around the agent invocation so an in-process builtin
    the agent calls (``set_conversation_mode``) reads the current conversation's thread from it. The
    completion continuation (:data:`COMPLETION_TOOL_NAME`) is bound for the run's duration, carrying
    this turn's ``thread_id`` and its intake ``message_id`` (the late-path deliverer rebuilds
    ``$turn`` from that stored record): it is the deferred-response delivery path that lets an async
    ask park here (a run with none bound refuses the ask loudly pre-persist), and a resumed run's
    final answer fires it to post the reply back into this thread. The outermost minting call frame is
    a PUSH of the target agent's name (the agent drive is not a tool dispatch, so nothing else pushes
    it), so the run's first ask records ``asked_by == [agent]``; it mints the run-delivery id and
    reads the bound completion as this run's out-of-band delivery address.
    """
    # ``completion_delivery`` imports this module's sibling, so the constant is read lazily to keep
    # the door↔turn package free of an import cycle.
    from tai42_skeleton.conversations.turn.completion_delivery import COMPLETION_TOOL_NAME

    agent = accessors._agent_registry().get(route.target_name)
    if agent is None:
        return _tool_error(f"agent {route.target_name!r} is not registered", route)
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
    parked = parked_entries_for_jq(await list_parked())
    try:
        contract = await evaluate_door_contract(route, payload, parked)
    except Exception as exc:
        logger.exception(
            "conversations: evaluating the door contract for route %r failed with %s",
            route.route_name,
            type(exc).__name__,
        )
        return _tool_error(f"door contract error ({type(exc).__name__})", route)
    try:
        run_kwargs, start_requested = _agent_run_kwargs(text, contract.start)
    except ValueError as exc:
        logger.warning(
            "conversations: start_expr for agent route %r produced invalid run kwargs: %s", route.route_name, exc
        )
        return _tool_error(f"start_expr error: {exc}", route)
    # A start runs only when the visit does not resume/take first (the pending-caller-ask guard
    # below refuses the actual start).
    if start_requested and not contract.resume and _has_pending_caller_ask(parked):
        logger.warning(
            "conversations: agent route %r cannot start a turn while a caller ask is pending", route.route_name
        )
        return _tool_error(
            "a caller question is still pending on this thread; the run cannot start until it is answered", route
        )
    turn_context = BridgeTurnContext(
        thread_id=thread_id,
        route_name=route.route_name,
        channel=route.channel,
        our_identity=route.our_identity,
        client_address=client_address,
    )

    async def _start(_extras: Any) -> Any:
        # The agent frame (opened below) carries the door's extras, so the drive reads them off the
        # ambient frame; the visit-validated ``extras`` argument is unused here.
        return await _drive_agent_terminal(agent, {**run_kwargs, "thread_id": thread_id})

    try:
        with bridge_turn_context(turn_context):
            async with bind_execution_identity(
                route.execution_key, bound_fingerprint=route.execution_key_fingerprint
            ) as identity:
                await authorize_execution_agent_run(identity, route.target_name)
                completion_token = set_park_completion(
                    COMPLETION_TOOL_NAME, {"thread_id": thread_id, "message_id": record.message_id}
                )
                try:
                    # The outermost minting call frame as a PUSH of the agent's name, under the
                    # bound identity and completion, carrying the door's extras: it mints the
                    # run-delivery id and reads the completion as the run's out-of-band address, and
                    # wraps the whole visit (a start OR a resume). The agent target carries no state
                    # binding, so ``visit`` deposits none.
                    with tool_call_frame(name=route.target_name, extras=contract.extras):
                        outcome = await visit(
                            target_name=route.target_name,
                            cancel=contract.cancel,
                            resume=contract.resume,
                            start=_start if start_requested else None,
                            extras=contract.extras,
                            receives_outcome=True,
                        )
                finally:
                    reset_park_completion(completion_token)
    except PermissionDeniedError as exc:
        return _tool_error(f"turn denied: {exc}", route)
    except Exception as exc:
        # A failed turn becomes a logged error OUTCOME, not a swallowed error.
        logger.exception("conversations: turn for route %r failed", route.route_name, exc_info=exc)
        return _tool_error(f"turn error: {exc}", route)
    return await _agent_outcome_of_visit(route, outcome, turn=cast("dict[str, object]", payload["turn"]), parked=parked)
