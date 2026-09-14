"""Select and run the route's target (tool / agent / manual / pairing) and build the
completed record the transition persists.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.conversations import ConversationRoute, Person
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
from tai42_skeleton.conversations.mode import effective_mode, supports_thread_append
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.pairing import Passthrough, classify
from tai42_skeleton.conversations.turn import accessors, agent_turn, pairing, tool_turn
from tai42_skeleton.conversations.turn.context import _conversation_attribution, _conversation_state_context
from tai42_skeleton.conversations.turn.outcome import _SilentOutcome, _tool_error, _ToolOutcome
from tai42_skeleton.conversations.turn.record import _outcome_record
from tai42_skeleton.conversations.turn.routing import _Multichannel
from tai42_skeleton.states.context import state_context
from tai42_skeleton.tools.attribution import run_attribution

logger = logging.getLogger("tai42_skeleton.conversations.turn")


async def _target_outcome(
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    person: Person | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> _ToolOutcome:
    """The route's TARGET turn as an outcome: a tool dispatch (which may be silent) or an
    agent run (always answered/error). The single dispatch both the plain and the
    multichannel paths route ordinary text to. ``person``, ``params``, ``form`` (the structured
    inbound submission), ``attachments`` (the participant's media) and ``location`` reach only the tool
    payload; the agent branch ignores them all — an agent target reads the rendered TEXT
    only, so a form-/media-unaware agent still sees the whole turn.

    A thread whose effective mode is ``manual`` runs NO target turn: this is where the
    suppression lives, so a pairing turn — dispatched on its own path, never through here —
    stays live and a first-contact greeting still prepends onto the silent outcome.

    The run's generic attribution is deposited HERE — the single seam both the tool and
    agent target turns route through — so whichever run chokepoint the target reaches (the
    tool ``run_tool`` seam or the agent ``drive_live_caller_astream`` seam) stamps its
    trace with this conversation's identity. tai42 stays flow-agnostic: it deposits only
    generic dimensions (a person-or-address user, the resolved thread as session, the
    route as a tag, the channel/our_identity as metadata) and interprets none of them."""
    if await effective_mode(route, intake.thread_id) == "manual":
        return await _manual_target_outcome(route, intake, text)
    attribution = _conversation_attribution(route, intake, person)
    context = _conversation_state_context(route, intake, person, actor=attribution.user_id)
    with run_attribution(attribution), state_context(context):
        if route.target_kind == "tool":
            return await tool_turn._run_tool_turn(
                route,
                text,
                intake.thread_id,
                intake.client_address,
                person,
                params,
                form,
                attachments,
                location,
                record=intake,
            )
        return await agent_turn._run_agent_turn(route, text, intake.thread_id, intake.client_address)


async def _manual_target_outcome(route: ConversationRoute, intake: ConversationRecord, text: str) -> _ToolOutcome:
    """The target turn SUPPRESSED for a manual-mode thread — no agent run, no tool dispatch.
    An agent target that HOLDS thread memory (implements ``append_thread_messages``) has the
    inbound appended to its checkpoint as a ``user`` message, so a later agent turn (once the
    thread returns to ``agent`` mode) reads it as prior context; a memoryless agent target
    (leaves the ABC default), an unregistered agent and a tool target have no thread memory to
    feed, so nothing is appended. Either way the turn produces no reply: the outcome is silent
    (terminal on the channel door, a delivered marker on the api door). An append that FAILS on
    a memory-holding target is a loud client-safe ``error`` outcome, never a silent skip that
    would drop the inbound out of the thread's memory unremarked. A turn that dies after the
    append takes an error outcome without re-running, so the redrive adds no duplicate; an
    api-door caller retrying the same inbound submits a fresh turn (new ``message_id``, no
    inbound dedup there) that appends the line again — accepted over losing the inbound from
    memory."""
    if route.target_kind == "agent":
        agent = accessors._agent_registry().get(route.target_name)
        if agent is not None and supports_thread_append(agent):
            try:
                await agent.append_thread_messages(
                    thread_id=intake.thread_id, messages=[{"role": "user", "content": text}]
                )
            except Exception as exc:
                logger.error(
                    "conversations: manual-mode inbound append for route %r failed", route.route_name, exc_info=exc
                )
                return _tool_error(f"manual-mode append error: {exc}", route)
    return _SilentOutcome()


async def _resolve_event_person(
    route: ConversationRoute, intake: ConversationRecord, multichannel: _Multichannel | None
) -> Person | None:
    """The EXISTING person an event turn carries in its tool payload, resolved READ-ONLY:
    a linked-person thread (``bridge:@person:{id}``) names it by id, otherwise a multichannel
    route reads it by the sending address. An event NEVER mints identity, so a thread with no
    such person carries no person fields — exactly like a plain, non-multichannel thread."""
    if intake.thread_id.startswith(PERSON_THREAD_PREFIX):
        return await accessors._person_store().get_by_id(intake.thread_id[len(PERSON_THREAD_PREFIX) :])
    if multichannel is None:
        return None
    return await accessors._person_store().get_person(
        multichannel.target,
        door=multichannel.door,
        channel=multichannel.channel,
        our_identity=multichannel.our_identity,
        address=multichannel.address,
    )


async def _resolve_turn_record(
    *,
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> ConversationRecord:
    """Run the route's target (or a pairing turn) and build the completed record the
    transition persists. With ``multichannel`` off this is byte-identical to the target turn.
    With it on, the canonical per-accept order runs at the HEAD of this scheduled
    execution — AFTER the door's terminal admission write: ``ensure_provisional`` (the sole
    person WRITE, keying the first-contact greeting and the redeem's own side) → classify
    → dispatch to the pairing turn or the target — and a due first-contact greeting is
    PREPENDED into the answer before it is persisted. ``params`` and ``form`` reach only a
    tool target's payload; a pairing turn ignores them. ``classify`` runs on the rendered
    TEXT (the commands match only the whole trimmed message, so a form's label:value lines
    can never classify as a command).

    An EVENT turn takes its own branch first: it resolves any existing person READ-ONLY
    (:func:`_resolve_event_person`) and carries the person fields into the tool payload exactly
    as a message turn would, but runs NONE of the person-WRITE path — no provisional mint, no
    greeting, no classify — because a structured event has no human sender to admit."""
    if intake.inbound_kind == "event":
        person = await _resolve_event_person(route, intake, multichannel)
        return _outcome_record(intake, await _target_outcome(route, intake, text, person, params, form))

    if multichannel is None:
        return _outcome_record(
            intake,
            await _target_outcome(
                route, intake, text, params=params, form=form, attachments=attachments, location=location
            ),
        )

    person, created = await accessors._person_store().ensure_provisional(
        multichannel.target, multichannel.address_row(), locale=intake.inbound_locale
    )
    greeting, greeting_code = await pairing._greeting_and_code(multichannel) if created else (None, None)
    action = classify(text)
    if isinstance(action, Passthrough):
        outcome = await _target_outcome(route, intake, text, person, params, form, attachments, location)
    else:
        # ``greeting_code`` is the greeting's already-minted code, if any: a first-contact
        # ``/link`` reuses it rather than minting a SECOND code that rotation would delete,
        # leaving the greeting carrying a now-dead code (a Redeem/Unlink ignore it).
        outcome = await pairing._run_pairing_turn(multichannel, person, action, greeting_code, route)
    return _outcome_record(intake, pairing._with_greeting(outcome, greeting))


async def _complete_turn(
    *,
    route: ConversationRoute,
    intake: ConversationRecord,
    text: str,
    multichannel: _Multichannel | None = None,
    params: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    attachments: list[MediaItem] | None = None,
    location: LocationElement | None = None,
) -> ConversationRecord:
    """Run the turn and move its intake record to its outcome (persist before send);
    delivery is the caller's to spawn. A produced answer goes to ``pending_delivery``; a
    silent tool turn goes straight to terminal ``silent`` with nothing to deliver. The
    transition is guarded on the record still being at intake, so a turn finishing after a
    re-drive resolved its record raises rather than overwriting the outcome the client was
    given."""
    completed = await _resolve_turn_record(
        route=route,
        intake=intake,
        text=text,
        multichannel=multichannel,
        params=params,
        form=form,
        attachments=attachments,
        location=location,
    )
    if completed.delivery_status is DeliveryStatus.SILENT:
        outcome = await accessors._store().complete_silent(completed)
        verb = "complete_silent"
    else:
        outcome = await accessors._store().complete_turn(completed)
        verb = "complete_turn"
    if outcome != 1:
        raise RuntimeError(
            f"conversations: record {intake.message_id} is no longer at intake "
            f"({verb} answered {outcome}); its outcome was resolved elsewhere and this turn's "
            "outcome is discarded"
        )
    return completed
