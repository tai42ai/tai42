"""Driving the generic resume continuation of an async ``ask`` park and delivering its terminal.

An async park stores a generic ``continuation_tool`` and the ``continuation_identity``
to run it as. When the park resolves — a human/callback ANSWER, or the expiry
reaper — the continuation runs bound to the stored identity (never the answerer's),
carrying only ``{interaction_id, ask_result}``. The seam is consumer-agnostic: it runs
a registered tool name under a rebound execution identity and knows nothing of the
driver's resume state. Firing the drive once per resolving claim, and redelivering it
from the durable continuation-due record, lives in
:mod:`tai42_skeleton.interactions.continuation_dispatch`.

The drive resumes the run, DELIVERS its terminal through the one platform ladder
(``drive_and_deliver`` → ``_deliver_terminal``: fire the run's out-of-band address, else
park a waiting outcome on its subject, else drop), and clears the continuation-due record
only after the terminal is delivered. A record still present past a healthy drive's window
is the reaper's signal to redeliver. Delivery is at-least-once — a redelivery (or a
slow-but-healthy drive the reaper laps) may re-drive the same run — so every ladder rung is
IDEMPOTENT, keyed by the run's per-run ``completion_id``: an address fire dedupes, a waiting
outcome writes once, and re-driving is harmless.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    PARK_COMPLETION_WITHDRAWN,
    AskAnswer,
    CallerAskLanding,
    DeliveryAddress,
    ParkRunTakenDownError,
    ResumeBuffered,
    RunFailed,
    RunTerminalFailed,
    SuspendedInteraction,
    declare_caller_ask_landing,
)
from tai42_contract.presets import PresetParkStages, preset_output_failed_outcome
from tai42_contract.states import StateBinding, StateContext, SubjectCandidates
from tai42_contract.tools import RunDelivery, run_delivery
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.interactions.park_giveup import ParkGiveUpFailure, fire_park_giveup
from tai42_kit.presets import PresetOutputError, run_output_stages
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.conversations.reply_to import resolve_ask_reply_to
from tai42_skeleton.interactions.run_completion import apply_deferred_binding, run_completion_id
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.runs.chokepoint import delivery_fire, resume_lineage, resume_origin
from tai42_skeleton.states.context import current_state_context, state_context
from tai42_skeleton.tools.stage_frames import current_drive_output_stages, drive_output_stages

# The out-of-band delivery address the platform captured on a resumed interaction: the door that
# started the run, or ``None`` for a receiver-less run.
_Delivery = DeliveryAddress | None

logger = logging.getLogger(__name__)

__all__ = ["drive_and_deliver"]


def _is_nonterminal(result: Any) -> bool:
    """Whether a resume's return is a STILL-PARKED step, not a terminal to deliver.

    A ``SuspendedInteraction`` (the run re-parked on a new ask) and a ``ResumeBuffered`` (sibling
    asks of the same super-step are still open) are both non-terminal: nothing is delivered and the
    new interaction carries the run's delivery forward.
    """
    return isinstance(result, (SuspendedInteraction, ResumeBuffered))


async def _ticket_stands(store: InteractionStore, interaction_id: str) -> bool:
    """Whether the resume's continuation-due ticket still stands; logs at INFO when it is gone.

    The kill seam revokes the ticket of every park of a run it takes down, so a receiver-less resume
    that was already driving when the kill landed reads it gone and delivers nothing.
    """
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        stands = await store.continuation_due_present(r, interaction_id)
    if not stands:
        logger.info(
            "interaction %s resumed to a terminal after its run was taken down; nothing is delivered", interaction_id
        )
    return stands


async def _clear_due(store: InteractionStore, interaction_id: str) -> None:
    """Clear the durable continuation-due record for a resolved resume.

    Opens its own client — a detached drive outlives the resolving door's connection.
    """
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        await store.clear_continuation_due(r, interaction_id)


@asynccontextmanager
async def _continuation_scope(
    identity: str,
    fingerprint: str | None,
    interaction_id: str,
    park_context: StateContext | None,
    caller_ask_landing: CallerAskLanding | None,
    chain_keys: Sequence[str],
    *,
    mark_detached: bool,
) -> AsyncIterator[None]:
    """Bind the frame a park's continuation work runs in: its identity, origin, lineage, state context and landing.

    Rebinds the park's stored execution ``identity`` (never the answerer's), names
    ``resume_origin(interaction_id)`` and deposits ``resume_lineage(chain_keys)`` — the chained
    calls the park recorded it is nested under, whose chain-delivery fires the resume authorization
    admits inside this frame — re-deposits the park's ``StateContext`` (so the resumed run's
    writes complete their provenance from the SAME door the park entered through) and RE-ESTABLISHES
    the park's caller-ask landing (so a re-ask is judged by the SAME door declaration the first ask
    was). ``mark_detached`` flags the work detached so a set synchronous turn budget never cancels a
    legitimately long out-of-band resume.
    """
    detached_token = mark_detached_run() if mark_detached else None
    try:
        park_ctx: AbstractContextManager[Any] = (
            state_context(park_context) if park_context is not None else nullcontext()
        )
        async with bind_execution_identity(identity, bound_fingerprint=fingerprint or ""):
            with (
                resume_origin(interaction_id),
                resume_lineage(chain_keys),
                park_ctx,
                declare_caller_ask_landing(caller_ask_landing),
            ):
                yield
    finally:
        if detached_token is not None:
            reset_detached_run(detached_token)


async def _run_continuation(
    identity: str,
    fingerprint: str | None,
    tool: str,
    interaction_id: str,
    answer: Any,
    park_context: StateContext | None = None,
    park_asked_by: Sequence[str] = (),
    caller_ask_landing: CallerAskLanding | None = None,
    chain_keys: Sequence[str] = (),
    *,
    reply_to_medium_message_id: str | None,
    params: dict[str, str] | None,
    mark_detached: bool = True,
) -> Any:
    """Run ``tool`` with ``{interaction_id, ask_result}`` in the park's continuation frame.

    ``ask_result`` is the value the parked call reads in place of the ask, always the
    :class:`~tai42_contract.interactions.AskAnswer` JSON a synchronous ask returns, its ``reply_to``
    resolved here from the ask's thread (so a redelivery reads the record as it is then). On an
    expiry its ``answer`` is the expiry marker and ``reply_to`` and ``params`` are null.

    The frame (:func:`_continuation_scope`) binds ``resume_origin(interaction_id)`` UNCONDITIONALLY
    (the origin names the interaction being resumed regardless of who receives the outcome); the
    parking run's call chain is restored (passed as ``continues_chain`` so the continuation tool's
    own name is not re-added), and the ``run_tool`` return value is ALWAYS taken and returned.

    An inline resume under a live caller passes ``mark_detached=False`` to run under that caller's
    budget. The run-DELIVERY context settle (re-establishing the stored context, or leaving the
    resumer's own in place) is the caller's — :func:`drive_and_deliver` binds it around this drive
    by ``receives_outcome``, with the park's captured output stages.

    Inside the frame, before the continuation runs, the park's answer stages map the AskAnswer JSON,
    as a preset's live output stage maps what its asking base returns; they do not run on an expiry
    or a failure. A refusal ends the park through its driver's give-up handlers in this same
    frame (:func:`_give_up_on_answer_stage`); the continuation never runs.
    """
    async with _continuation_scope(
        identity, fingerprint, interaction_id, park_context, caller_ask_landing, chain_keys, mark_detached=mark_detached
    ):
        ask_result = await _ask_answer_json(interaction_id, answer, reply_to_medium_message_id, params)
        stages = current_drive_output_stages()
        if stages is not None and stages.answer and not _is_expiry_or_failure(answer):
            refusal: PresetOutputError | None = None
            try:
                ask_result = await run_output_stages(stages.answer, ask_result)
            except PresetOutputError as exc:
                refusal = exc
            if refusal is not None:
                return await _give_up_on_answer_stage(interaction_id, refusal)
        arguments = {"interaction_id": interaction_id, "ask_result": ask_result}
        return await tai42_app.tools.run_tool(tool, arguments, continues_chain=park_asked_by)


def _is_expiry_or_failure(answer: Any) -> bool:
    """Whether ``answer`` is the expiry marker or a failure, which the answer stages do not map."""
    return answer == EXPIRY_ANSWER or isinstance(answer, RunFailed)


async def _ask_answer_json(
    interaction_id: str, answer: Any, reply_to_medium_message_id: str | None, params: dict[str, str] | None
) -> dict[str, Any]:
    """The ``AskAnswer`` JSON a synchronous ask returns for ``answer``, its quoted message resolved."""
    reply_to = (
        None
        if reply_to_medium_message_id is None
        else await resolve_ask_reply_to(interaction_id, reply_to_medium_message_id)
    )
    return AskAnswer(answer=answer, reply_to=reply_to, params=params).model_dump(mode="json")


async def _give_up_on_answer_stage(interaction_id: str, refusal: PresetOutputError) -> Any:
    """End a park whose answer stage refused the answer, through its driver's give-up handlers.

    Each handler is handed the refusal as a :class:`ParkGiveUpFailure` (an upstream error, no
    transport). A handler's returned ``RunFailed`` is the run's failed terminal, raised as
    :class:`RunTerminalFailed` with its ``reply``; any other return is the drive's result. With
    no handler owning the park the run fails with the preset's FAILED outcome and no ``reply``.
    """
    text = str(refusal)
    outcome = preset_output_failed_outcome(refusal.preset, text)
    handled = await fire_park_giveup(
        interaction_id, ParkGiveUpFailure(outcome, text, "PresetOutputError", ErrorKind.UPSTREAM_ERROR, False)
    )
    if handled is None:
        raise RunTerminalFailed(outcome)
    if isinstance(handled.result, RunFailed):
        raise RunTerminalFailed(handled.result.outcome, reply=handled.result.reply)
    return handled.result


async def apply_terminal_stages(stages: PresetParkStages | None, result: Any) -> Any:
    """``result`` with the park's terminal stages applied; a refusal raises the run's :class:`RunTerminalFailed`.

    A ``RunFailed`` and a re-park (:func:`_is_nonterminal`) pass through untouched.
    """
    if stages is None or not stages.terminal or _is_nonterminal(result):
        return result
    try:
        return await run_output_stages(stages.terminal, result)
    except PresetOutputError as exc:
        raise RunTerminalFailed(preset_output_failed_outcome(exc.preset, str(exc))) from exc


async def _deliver_terminal(
    store: InteractionStore,
    *,
    interaction_id: str,
    outcome: Any,
    status: str,
    delivery: _Delivery,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
    reply: str | None = None,
) -> None:
    """Deliver a run's TERMINAL through the ladder — fire the address, else subject-track, else drop.

    The address rung fires the door's address with ``status`` for every terminal. The subject rung
    parks a waiting outcome for a ``succeeded`` or ``failed`` terminal; a ``withdrawn`` terminal (the
    platform took the run down on purpose) has no outcome for anyone to take, so it only leaves the
    subject index. Every rung is IDEMPOTENT, keyed by the run's ``completion_id`` (from
    ``run_delivery_id``): the address fire dedupes on it (``completion_delivery``), the waiting
    outcome writes once, so a redelivery of the same terminal lands once. A ``withdrawn`` fire
    anchors no record: a run that continues after a kill inside its own chain re-parks under the
    same id and its real terminal is still delivered. An interaction reaching delivery with NO
    stored ``run_delivery_id`` RAISES loudly — a park that stored none is a platform bug, never a
    per-interaction fallback id. A transient failure of the fire propagates, so the caller keeps the
    due record for the reaper.

    ``reply`` is the participant-facing text a target's failure carries
    (:class:`~tai42_contract.interactions.RunTerminalFailed` / :class:`RunFailed` ``reply``): the
    address fire carries it as the payload's ``reply`` key on a ``failed`` terminal, and the key is
    absent when there is none. Only a failed terminal carries a reply, so a reply on any other
    status RAISES (a platform bug). The subject rung stores only the opaque ``outcome``.
    """
    if reply is not None and status != PARK_COMPLETION_FAILED:
        raise ValueError(
            f"only a failed terminal carries a reply; interaction {interaction_id!r} has status {status!r}"
        )
    if run_delivery_id is None:
        raise RuntimeError(
            f"interaction {interaction_id!r} reached delivery with no stored run_delivery_id "
            "(a parkable run always mints one at its outermost start)"
        )
    completion_id = run_completion_id(run_delivery_id)
    if delivery is not None and delivery.tool is not None:
        payload = {
            **(delivery.context or {}),
            "thread_id": delivery.thread_id,
            "result": outcome,
            "completion_id": completion_id,
            "status": status,
        }
        if reply is not None:
            payload["reply"] = reply
        # Fire the door's address under the platform's delivery-fire context so the address tool
        # passes ``assert_delivery_authorized`` — the ONE place an address is fired.
        with delivery_fire(completion_id):
            await tai42_app.tools.run_tool(delivery.tool, payload)
        # The run is complete: drop its ``running`` subject-index membership (an answered park kept
        # it while the run drove). The subject-track rung does this through ``add_outcome``; the
        # address rung must do it too, after the fire so a fire failure leaves it for a redelivery.
        if candidates is not None and candidates.by_kind:
            async with client_ctx(RedisClient, interactions_settings().redis) as r:
                await store.leave_subject_index(r, interaction_id=interaction_id, candidates=candidates)
        return
    if candidates is not None and candidates.by_kind and status == PARK_COMPLETION_WITHDRAWN:
        # A deliberate takedown with no address: nothing to take, so no waiting outcome — only the
        # run's ``running`` subject-index membership leaves (a pending park's prune already left it).
        async with client_ctx(RedisClient, interactions_settings().redis) as r:
            await store.leave_subject_index(r, interaction_id=interaction_id, candidates=candidates)
        return
    if candidates is not None and candidates.by_kind:
        # No address, but a subject: park the terminal for whoever next runs on the subject. Its
        # Redis TTL is a BACKSTOP at twice the retention horizon; the reaper's retention sweep drops
        # it one horizon out with a loud event, so it never leaves silently by the TTL alone.
        async with client_ctx(RedisClient, interactions_settings().redis) as r:
            await store.add_outcome(
                r,
                completion_id=completion_id,
                interaction_id=interaction_id,
                status="finished" if status == PARK_COMPLETION_SUCCEEDED else "failed",
                result=outcome,
                candidates=candidates,
                retention_ttl=2 * interactions_settings().idle_ttl_seconds,
                run_delivery_id=run_delivery_id,
            )
        return
    # Nobody waits on this run and nothing tracks it (a receiver-less run with no subject): the
    # outcome has no destination. A legitimate terminal, logged rather than delivered.
    logger.info(
        "interaction %s reached a terminal with no delivery address and no subject to track; outcome dropped",
        interaction_id,
    )


async def _deliver_receiverless_failure(
    store: InteractionStore,
    *,
    interaction_id: str,
    failure: RunTerminalFailed,
    delivery: DeliveryAddress | None,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
) -> None:
    """Deliver a receiver-less drive's FAILED terminal through the ladder while its ticket stands, then clear it."""
    if not await _ticket_stands(store, interaction_id):
        return
    await _deliver_terminal(
        store,
        interaction_id=interaction_id,
        outcome=failure.outcome,
        status=PARK_COMPLETION_FAILED,
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=candidates,
        reply=failure.reply,
    )
    await _clear_due(store, interaction_id)


async def _deliver_receiverless_outcome(
    store: InteractionStore,
    *,
    interaction_id: str,
    result: Any,
    delivery: DeliveryAddress | None,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
    deferred_binding: StateBinding | None,
    run_input: Mapping[str, Any] | None,
    door_id: str | None,
) -> None:
    """Deliver a receiver-less drive's terminal (:func:`_deliver_run_outcome`) while its ticket stands; clear it."""
    if not await _ticket_stands(store, interaction_id):
        return
    await _deliver_run_outcome(
        store,
        interaction_id=interaction_id,
        result=result,
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=candidates,
        deferred_binding=deferred_binding,
        run_input=run_input,
        door_id=door_id,
    )
    await _clear_due(store, interaction_id)


async def drive_and_deliver(
    store: InteractionStore,
    *,
    identity: str,
    fingerprint: str | None,
    tool: str,
    interaction_id: str,
    answer: Any,
    reply_to_medium_message_id: str | None,
    params: dict[str, str] | None,
    park_context: StateContext | None,
    park_asked_by: Sequence[str],
    caller_ask_landing: CallerAskLanding | None = None,
    chain_keys: Sequence[str] = (),
    delivery: _Delivery,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
    receives_outcome: bool,
    deferred_binding: StateBinding | None = None,
    run_input: Mapping[str, Any] | None = None,
    door_id: str | None = None,
    output_stages: PresetParkStages | None = None,
) -> Any:
    """The ONE delivery chokepoint every continuation runs through — drive, then deliver its terminal.

    Settles the run-DELIVERY context by ``receives_outcome`` and drives the continuation
    (``_run_continuation``), then delivers what comes back:

    * WITHOUT a live receiver (``receives_outcome=False`` — the detached answer/expiry/reaper drive)
      it re-establishes the resumed interaction's OWN stored run-delivery context around the drive
      (``run_delivery(RunDelivery(...))``), so the run keeps its ``run_delivery_id`` and address and
      a re-park stores the same pair; WITH a live receiver (``receives_outcome=True`` — a ``visit``
      adoption) it re-establishes NOTHING, leaving the resumer's own ambient context in place.
    * a non-terminal re-park / buffered step stays parked — nothing is delivered, the due record is
      cleared, and the value is returned for a caller (``visit``) to normalise;
    * a terminal WITH a live inline receiver is RETURNED to it, nothing is fired;
    * a terminal WITHOUT a receiver runs the ladder (``_deliver_terminal``: fire the address, else
      subject-track, else drop);
    * a ``RunTerminalFailed`` is a TERMINAL to deliver FAILED — with a live receiver it clears the
      due record and propagates; without one it delivers FAILED through the ladder, its ``outcome``
      the opaque payload the delivery carries whole;
    * a PLAIN raise is keyed on ``receives_outcome``: a live receiver clears the due
      record and the error propagates to the resumer; a receiver-less drive KEEPS the record and
      surfaces the error loudly (the reaper redelivers).

    A non-failing RETURN is always a SUCCESS (a failure is the ``RunTerminalFailed`` raise, never a
    returned status-keyed value), so a delivered terminal carries ``PARK_COMPLETION_SUCCEEDED``
    without any inspection of the returned value.

    On a real terminal SUCCESS — both branches, a live inline receiver and a receiver-less drive —
    the run's DEFERRED door binding updates apply over the terminal output (``apply_deferred_binding``),
    so a run that parked to ask writes the door updates the pause dropped; never on a
    ``RunTerminalFailed`` or a re-park, matching the live apply which runs only on a clean output.

    A receiver-less terminal is delivered only while the resume's continuation-due ticket stands;
    the kill seam clears the ticket of every park it reaches — siblings and nested runs alike — so
    a resume that was already driving when the kill landed delivers nothing. A drive the facet
    refuses because its run was taken down (``ParkRunTakenDownError``) propagates to a live
    receiver and, receiver-less, delivers nothing and is logged at INFO.

    ``output_stages`` are the preset output stages the park captured, bound around the drive
    (:func:`~tai42_skeleton.tools.stage_frames.drive_output_stages`): the answer stages map the
    ``AskAnswer`` JSON before the continuation receives it as its ``ask_result``, the chained
    stages settle a chained run's terminal fired inside the drive, and the terminal stages map a
    terminal success before the deferred binding reads it and before it is delivered; a terminal
    stage's refusal is a ``RunTerminalFailed`` handled as above.

    The due record is cleared only AFTER a terminal is delivered, so a transient delivery failure
    leaves it for the reaper. Returns the inline receiver's outcome (or the non-terminal value); a
    receiver-less drive returns ``None``.
    """
    from tai42_skeleton.tools.state_binding import deferred_binding_scope

    run_delivery_ctx: AbstractContextManager[Any] = (
        nullcontext()
        if receives_outcome
        else run_delivery(RunDelivery(run_delivery_id, delivery) if run_delivery_id is not None else None)
    )
    # Re-establish the run's deferred door binding around the resume drive, EXACTLY as the delivery
    # address is re-established above: so a run that asks AGAIN mid-drive re-captures the SAME per-run
    # binding onto the re-park (``resolve_async_continuation`` reads it off this scope), rather than
    # losing it — the generic resume tool carries no binding of its own. The final terminal applies it
    # once, keyed on the run's unchanged ``completion_id``.
    deferred_ctx: AbstractContextManager[Any] = (
        deferred_binding_scope(deferred_binding, dict(run_input), door_id)
        if deferred_binding is not None and run_input is not None and door_id is not None
        else nullcontext()
    )
    park_failed: RunTerminalFailed | None = None
    try:
        with run_delivery_ctx, deferred_ctx, drive_output_stages(output_stages):
            result = await _run_continuation(
                identity,
                fingerprint,
                tool,
                interaction_id,
                answer,
                park_context,
                park_asked_by,
                caller_ask_landing,
                chain_keys,
                reply_to_medium_message_id=reply_to_medium_message_id,
                params=params,
                mark_detached=not receives_outcome,
            )
            result = await apply_terminal_stages(output_stages, result)
    except RunTerminalFailed as exc:
        if receives_outcome:
            # A live receiver owns the failure: clear the record and propagate it to the resumer.
            await _clear_due(store, interaction_id)
            raise
        park_failed = exc
        result = None
    except ParkRunTakenDownError:
        if receives_outcome:
            # A live receiver learns the run was taken down: clear the record and propagate.
            await _clear_due(store, interaction_id)
            raise
        # The kill's revoke already cleared this park's ticket: nothing is delivered or kept.
        logger.info(
            "interaction %s: its run was taken down while its answer was in flight; nothing is delivered",
            interaction_id,
        )
        return None
    except Exception:
        # A PLAIN raise: a live receiver clears the record and the error propagates; a
        # receiver-less drive KEEPS the record (never cleared here) and surfaces loudly, so the
        # reaper redelivers idempotently — the only path that does not lose a receiver-less outcome.
        if receives_outcome:
            await _clear_due(store, interaction_id)
        raise

    if park_failed is not None:
        await _deliver_receiverless_failure(
            store,
            interaction_id=interaction_id,
            failure=park_failed,
            delivery=delivery,
            run_delivery_id=run_delivery_id,
            candidates=candidates,
        )
        return None

    if _is_nonterminal(result):
        await _clear_due(store, interaction_id)
        return result

    if receives_outcome:
        # A live inline receiver takes the run's outcome; nothing is fired. The run's deferred door
        # updates apply over the terminal output before the record is cleared.
        await apply_deferred_binding(
            deferred_binding=deferred_binding,
            run_input=run_input,
            door_id=door_id,
            output=result,
            run_delivery_id=run_delivery_id,
        )
        await _clear_due(store, interaction_id)
        return result

    await _deliver_receiverless_outcome(
        store,
        interaction_id=interaction_id,
        result=result,
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=candidates,
        deferred_binding=deferred_binding,
        run_input=run_input,
        door_id=door_id,
    )
    return None


async def _deliver_run_outcome(
    store: InteractionStore,
    *,
    interaction_id: str,
    result: Any,
    delivery: _Delivery,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
    deferred_binding: StateBinding | None,
    run_input: Mapping[str, Any] | None,
    door_id: str | None,
) -> None:
    """Deliver a run's outermost outcome to the run's address with no live receiver.

    A still-parked step (a re-park, a buffered step) delivers nothing; a :class:`RunFailed` delivers
    FAILED with its outcome and its ``reply``; any other value is a SUCCESS: the run's deferred door binding updates
    apply over it, then SUCCEEDED is delivered.
    """
    if _is_nonterminal(result):
        return
    if isinstance(result, RunFailed):
        await _deliver_terminal(
            store,
            interaction_id=interaction_id,
            outcome=result.outcome,
            status=PARK_COMPLETION_FAILED,
            delivery=delivery,
            run_delivery_id=run_delivery_id,
            candidates=candidates,
            reply=result.reply,
        )
        return
    await apply_deferred_binding(
        deferred_binding=deferred_binding,
        run_input=run_input,
        door_id=door_id,
        output=result,
        run_delivery_id=run_delivery_id,
    )
    await _deliver_terminal(
        store,
        interaction_id=interaction_id,
        outcome=result,
        status=PARK_COMPLETION_SUCCEEDED,
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=candidates,
    )


def _current_state_context_for_park() -> StateContext | None:
    """The ambient state context a park records for its resume, or ``None`` when the park ran under no state context.

    The resume re-deposits the original door's subject + write provenance. Read at request-build time, inside the
    parking turn's own context.
    """
    return current_state_context()
