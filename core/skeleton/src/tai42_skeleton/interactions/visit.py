"""The shared visit — the ONE seam every door drives a parkable run through.

A door (a conversation route, a hook, a schedule, a direct run-tool call, an agent SSE stream)
resolves its own jqs to plain values and hands them to :func:`visit`, which runs the whole
cancel / resume / take / start order ONCE for every caller, in this fixed order:

1. Check EVERYTHING before doing anything — every id names a live entry of the run's own
   parked list; ``cancel`` ∩ ``resume`` is refused; resume items are all ``to="caller"``, ``asking``
   and belong to ONE step of ONE held run; a payload on a ``finished``/``failed`` entry is refused;
   each resume payload passes the ask's declared answer check; a resume carrying non-empty extras is
   refused; the start's extras keys are declared by the target. A structural refusal or an answer
   mismatch raises with NOTHING cancelled and NOTHING resumed.
2. Cancel — each id through the one whole-chain kill seam.
3. Resume or take (at most one action besides cancel) — a resume claims the answer atomically and
   drives the continuation INLINE through the one delivery chokepoint
   (:func:`~tai42_skeleton.interactions.continuation.drive_and_deliver`); a take atomically claims a
   waiting outcome (``finished`` → its result, ``failed`` → re-raised as
   :class:`~tai42_contract.interactions.ParkableRunFailedError`).
4. Start — only when nothing was resumed or taken and ``start`` is not ``None``: ``await
   start(extras)`` under the door's ``state_binding`` (deposited as a
   :class:`~tai42_contract.tools.ToolInvocation` ONLY here, never around a resume/take/cancel
   continuation).
5. Normalise what came back (from a start or a resume) into caller asks / a re-park / a final
   result — classified PER ASK from the sentinel's two id lists (a ``SuspendedInteraction``) or the
   store's ``to`` (a ``ResumeBuffered``), never one representative ask.

The delivery of a resumed run's terminal is the platform's, settled by ``receives_outcome`` inside
the chokepoint: a live inline receiver gets it RETURNED (nothing fired); a receiver-less caller (a
hook or a schedule resuming a ``to="caller"`` entry) has none, so the chokepoint fires the run's
address or subject-tracks it.

The generic ``list_parked`` / ``resume_parked`` / ``cancel_parked`` here are thin wrappers over the
same visit internals, so a plugin drives parked runs without ``run_tool``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionResponse,
    ParkableRunFailedError,
    ParkedEntry,
    ParkedEntryGoneError,
    QuestionFormat,
    ResumeBuffered,
    ResumeItem,
    SuspendedInteraction,
    TakeItem,
    UndeclaredExtrasKeyError,
    VisitOutcome,
    VisitRequestError,
)
from tai42_contract.states import StateContext
from tai42_contract.tools import ToolInvocation, reset_current_tool_invocation, set_current_tool_invocation
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.answer_check import check_answer
from tai42_skeleton.interactions.continuation import continuation_due_timing, drive_and_deliver
from tai42_skeleton.interactions.kill import kill_park
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.runs.chokepoint import note_resumed_interaction
from tai42_skeleton.states.context import current_state_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from tai42_contract.interactions import InteractionRequest
    from tai42_contract.states import SubjectCandidates
    from tai42_contract.states.binding import StateBinding


# The sentinel distinguishing ``resume_parked``'s "no payload given → take" from a real ``None``
# answer payload. It never appears in a stored payload.
_TAKE = object()

# The answerer recorded for a caller resume: the resolving run, not a human.
_RESUMED_BY_CALLER = "caller"

# What ``visit`` classifies a start/resume return into (the ``VisitOutcome.kind`` values).
_Kind = Literal["result", "asks", "parked", "none"]


async def visit(
    *,
    target_name: str,
    cancel: list[str],
    resume: list[ResumeItem | TakeItem],
    start: Callable[[Mapping[str, Any]], Awaitable[Any]] | None,
    extras: Mapping[str, Any],
    state_binding: StateBinding | None = None,
    receives_outcome: bool = True,
) -> VisitOutcome:
    """Drive one door's parkable run through the shared cancel / resume / take / start order.

    See the module docstring for the exact order. Returns a :class:`VisitOutcome`; raises a
    :class:`~tai42_contract.interactions.VisitRequestError` /
    :class:`~tai42_contract.interactions.AnswerMismatchError` /
    :class:`~tai42_contract.interactions.ParkedEntryGoneError` /
    :class:`~tai42_contract.interactions.UndeclaredExtrasKeyError` from the pre-check with nothing
    cancelled and nothing resumed, or a :class:`~tai42_contract.interactions.ParkableRunFailedError`
    from a take of a ``failed`` outcome.
    """
    resumes = [item for item in resume if isinstance(item, ResumeItem)]
    takes = [item for item in resume if isinstance(item, TakeItem)]

    settings = interactions_settings() if interactions_store_configured() else None
    store = InteractionStore(settings.key_prefix) if settings is not None else None
    ctx = current_state_context()
    candidates = ctx.candidates if ctx is not None else None

    # --- 1. Check EVERYTHING before doing anything. Every raise below leaves the store
    #        untouched: nothing is cancelled and nothing is resumed. ---
    if resume and extras:
        raise VisitRequestError("resume_extras", "a resume carries no extras — extras are for a start only")
    overlap = sorted(set(cancel) & {item.id for item in resume})
    if overlap:
        raise VisitRequestError(
            "cancel_resume_overlap", f"an id cannot be both cancelled and resumed in one visit: {overlap}"
        )
    if resumes and takes:
        raise VisitRequestError(
            "multi_run", "a visit resolves one held run: a live resume and a settled take cannot share one visit"
        )

    parked = await _parked_index(store, settings, candidates) if (cancel or resume) else {}
    for iid in (*cancel, *(item.id for item in resume)):
        if iid not in parked:
            raise ParkedEntryGoneError(f"interaction {iid!r} is not parked on this run's subject")
    _check_resumes(resumes, parked)
    _check_takes(takes, parked)
    if extras:
        await _assert_extras_declared(target_name, extras)

    # --- 2. Cancel (each id → the one whole-chain kill seam). ---
    cancelled = await _cancel_all(store, settings, cancel, parked)

    # --- 3/4/5. At most one action besides cancel, then normalise what came back. ---
    if resumes:
        result = await _drive_resumes(store, settings, ctx, candidates, resumes, parked, receives_outcome)
        kind, value, asks, suspended = await _normalise(store, settings, candidates, result)
        return VisitOutcome(
            action="resumed", cancelled=cancelled, kind=kind, result=value, asks=asks, suspended=suspended
        )
    if takes:
        value = await _take_all(store, settings, takes)
        return VisitOutcome(action="taken", cancelled=cancelled, kind="result", result=value)
    if start is not None:
        started = await _run_start(target_name, start, extras, state_binding)
        kind, value, asks, suspended = await _normalise(store, settings, candidates, started)
        return VisitOutcome(
            action="started", cancelled=cancelled, kind=kind, result=value, asks=asks, suspended=suspended
        )
    return VisitOutcome(action="none", cancelled=cancelled, kind="none")


# --- the pre-check ---------------------------------------------------------------------


def _check_resumes(resumes: list[ResumeItem], parked: dict[str, dict[str, Any]]) -> None:
    """Raise for any resume item that is not a live ``to="caller"``, ``asking`` ask of ONE held step.

    Every entry exists (the id guard already ran). A ``to="user"`` id, a payload on a
    ``finished``/``failed`` entry, resume items spanning two steps/runs, or a payload the ask's
    declared answer format rejects each raise here BEFORE anything is cancelled or resumed.
    """
    if not resumes:
        return
    steps: set[tuple[str | None, tuple[str, ...] | None]] = set()
    for item in resumes:
        entry = parked[item.id]
        if entry.get("to") == "user":
            raise VisitRequestError(
                "user_ask_in_resume", f"interaction {item.id!r} was sent to the user; a caller can never resume it"
            )
        status = entry["status"]
        if status in ("finished", "failed"):
            raise VisitRequestError(
                "finished_payload", f"interaction {item.id!r} already resolved ({status}); a payload cannot resume it"
            )
        if status != "asking":
            raise ParkedEntryGoneError(f"interaction {item.id!r} is no longer asking (status {status!r})")
        asked_by = tuple(entry["asked_by"]) if entry.get("asked_by") is not None else None
        steps.add((entry.get("group_id"), asked_by))
        check_answer(
            QuestionFormat(
                answer_format=AnswerFormat(entry["answer_format"]), format_payload=entry.get("format_payload")
            ),
            item.payload,
        )
    if len(steps) > 1:
        raise VisitRequestError(
            "multi_run", "resume items must belong to ONE step of ONE held run (same group_id and asked_by)"
        )


def _check_takes(takes: list[TakeItem], parked: dict[str, dict[str, Any]]) -> None:
    """Raise for any take whose entry is not a resolved (``finished``/``failed``) waiting outcome."""
    for item in takes:
        status = parked[item.id]["status"]
        if status not in ("finished", "failed"):
            raise VisitRequestError(
                "finished_payload", f"interaction {item.id!r} has no waiting outcome to take (status {status!r})"
            )


async def _assert_extras_declared(target_name: str, extras: Mapping[str, Any]) -> None:
    """Raise :class:`UndeclaredExtrasKeyError` for any extras key the target does not declare reading."""
    declared = await tai42_app.tools.declared_extras(target_name)
    undeclared = sorted(set(extras) - set(declared))
    if undeclared:
        raise UndeclaredExtrasKeyError(
            f"tool {target_name!r} does not declare extras keys {undeclared} (declared: {sorted(declared)})"
        )


# --- the parked-list hand-over -------------------------------------------------


async def _parked_index(
    store: InteractionStore | None, settings: Any, candidates: SubjectCandidates | None
) -> dict[str, dict[str, Any]]:
    """The run's own parked list as ``{id: entry}`` — the pre-check's id match reads it."""
    if store is None or candidates is None:
        return {}
    async with client_ctx(RedisClient, settings.redis) as r:
        entries = await store.list_parked_for(r, candidates)
    return {entry["id"]: entry for entry in entries}


# --- cancel -----------------------------------------------------------------------


async def _cancel_all(
    store: InteractionStore | None, settings: Any, cancel: list[str], parked: dict[str, dict[str, Any]]
) -> list[str]:
    """Whole-chain kill each cancel id through the one teardown seam; return the ids acted on."""
    if not cancel or store is None:
        return list(cancel)
    async with client_ctx(RedisClient, settings.redis) as r:
        for iid in cancel:
            await kill_park(r, store, iid, parked[iid].get("group_id") or "", reason="cancelled")
    return list(cancel)


# --- resume -----------------------------------------------------------------------


async def _drive_resumes(
    store: InteractionStore | None,
    settings: Any,
    ctx: StateContext | None,
    candidates: SubjectCandidates | None,
    resumes: list[ResumeItem],
    parked: dict[str, dict[str, Any]],
    receives_outcome: bool,
) -> Any:
    """Claim each sibling answer and drive it inline through the ONE chokepoint; return the LAST outcome.

    The parallel siblings of one step buffer until the last resolves: each drive but the last returns
    a ``ResumeBuffered``; the last returns the step's terminal (or a re-park). The last drive's return
    is what the visit normalises.
    """
    if store is None:
        raise ParkedEntryGoneError("no interactions store is configured")
    result: Any = None
    for item in resumes:
        result = await _resume_one(store, settings, ctx, candidates, item, receives_outcome)
        # The answer was claimed and its continuation driven: record this id on the
        # ambient run record's resumed-interaction list. A pre-check refusal raises
        # before ever reaching here, so nothing is recorded for a refused visit.
        note_resumed_interaction(item.id)
    return result


async def _resume_one(
    store: InteractionStore,
    settings: Any,
    ctx: StateContext | None,
    candidates: SubjectCandidates | None,
    item: ResumeItem,
    receives_outcome: bool,
) -> Any:
    """Atomically claim ``item``'s answer and drive its continuation inline through the chokepoint.

    The claim MULTI writes the durable continuation-due record whose ``delivery`` and
    ``run_delivery_id`` are the run's stored values; a lost claim raises
    :class:`ParkedEntryGoneError`. The continuation then runs INLINE, awaited, through
    :func:`drive_and_deliver`, which binds ``resume_origin`` unconditionally and settles delivery by
    ``receives_outcome``. The deposited state context is MIXED — the resumer's door/turn/actor with
    the PARK's subject candidates.
    """
    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, item.id)
        if state is None:
            raise ParkedEntryGoneError(f"interaction {item.id!r} vanished before its resume claim")
        request = state.request
        response = InteractionResponse(
            interaction_id=item.id,
            answer=item.payload,
            answered_by=_RESUMED_BY_CALLER,
            answered_at=_now(),
        )
        due_ttl, due_first_attempt_at_ms = continuation_due_timing(settings)
        claimed = await store.record_answer(
            r,
            response,
            state.group_id,
            _reply_ttl(request),
            continuation_due_ttl=due_ttl,
            continuation_first_attempt_at_ms=due_first_attempt_at_ms,
        )
        if not claimed:
            raise ParkedEntryGoneError(f"interaction {item.id!r} was resolved by another caller before this resume")
        fingerprint = await store.continuation_fingerprint(r, item.id)

    park_candidates = (
        request.continuation_state_context.candidates if request.continuation_state_context is not None else candidates
    )
    return await drive_and_deliver(
        store,
        identity=request.continuation_identity or "",
        fingerprint=fingerprint,
        tool=request.continuation_tool or "",
        interaction_id=item.id,
        answer=item.payload,
        park_context=_mixed_context(ctx, park_candidates, request),
        park_asked_by=request.asked_by,
        delivery=request.delivery,
        run_delivery_id=request.run_delivery_id,
        candidates=park_candidates,
        receives_outcome=receives_outcome,
    )


def _mixed_context(
    ctx: StateContext | None, park_candidates: SubjectCandidates | None, request: InteractionRequest
) -> StateContext | None:
    """The state context the resumed run writes under: the resumer's door/turn/actor + the PARK's candidates.

    So a resumed run's writes land on the park's subject while their provenance names the door
    the RESUMER entered through. Falls back to the park's own stored context when the resumer
    ran under none.
    """
    if ctx is not None and park_candidates is not None:
        return StateContext(
            door=ctx.door,
            candidates=park_candidates,
            actor=ctx.actor,
            turn_id=ctx.turn_id,
            inbound_id=ctx.inbound_id,
        )
    return request.continuation_state_context


# --- take ------------------------------------------------------------------


async def _take_all(store: InteractionStore | None, settings: Any, takes: list[TakeItem]) -> Any:
    """Atomically take each waiting outcome; return the LAST result (``failed`` re-raises)."""
    if store is None:
        raise ParkedEntryGoneError("no interactions store is configured")
    result: Any = None
    async with client_ctx(RedisClient, settings.redis) as r:
        for item in takes:
            outcome = await store.claim_outcome(r, item.id)
            if outcome is None:
                raise ParkedEntryGoneError(f"interaction {item.id!r} has no waiting outcome to take (already taken)")
            # The waiting outcome was atomically claimed (taken): record this id on the
            # ambient run record's resumed-interaction list, then surface a ``failed``
            # outcome to the caller. A miss above raises before recording anything.
            note_resumed_interaction(item.id)
            if outcome.status == "failed":
                raise ParkableRunFailedError(outcome.result)
            result = outcome.result
    return result


# --- start -----------------------------------------------------------------


async def _run_start(
    target_name: str,
    start: Callable[[Mapping[str, Any]], Awaitable[Any]],
    extras: Mapping[str, Any],
    state_binding: StateBinding | None,
) -> Any:
    """Run ``start(extras)`` under the door's ``state_binding``, deposited ONLY here.

    The binding rides the ambient invoked-tool seam to the dispatch chokepoint, which applies it
    around the started target and never around a resume/take/cancel continuation. ``None`` deposits
    nothing (every agent target, a bindingless door). Token discipline: reset in the ``finally``.
    """
    if state_binding is None:
        return await start(extras)
    token = set_current_tool_invocation(ToolInvocation(tool_name=target_name, state_binding=state_binding))
    try:
        return await start(extras)
    finally:
        reset_current_tool_invocation(token)


# --- normalise what came back ----------------------------------------------


async def _normalise(
    store: InteractionStore | None, settings: Any, candidates: SubjectCandidates | None, value: Any
) -> tuple[_Kind, Any, list[ParkedEntry], SuspendedInteraction | None]:
    """Classify a start/resume return into ``(kind, result, asks, suspended)`` PER ASK.

    A ``SuspendedInteraction`` with caller asks → ``asks`` (their full entries); with only user asks
    → ``parked`` (the sentinel). A ``ResumeBuffered`` partitions ``remaining_ids`` by the store's
    ``to`` → ``asks`` for any caller ask left, else ``parked`` with a sentinel over the user ids.
    ``None`` (a receiver-less terminal already delivered out of band) → ``none``; anything else is
    the final result.
    """
    if isinstance(value, SuspendedInteraction):
        return await _normalise_suspended(store, settings, candidates, value)
    if isinstance(value, ResumeBuffered):
        return await _normalise_buffered(store, settings, candidates, value)
    if value is None:
        return "none", None, [], None
    return "result", value, [], None


async def _normalise_suspended(
    store: InteractionStore | None,
    settings: Any,
    candidates: SubjectCandidates | None,
    sentinel: SuspendedInteraction,
) -> tuple[_Kind, Any, list[ParkedEntry], SuspendedInteraction | None]:
    """A ``SuspendedInteraction`` → ``asks`` for its caller ids, else ``parked`` with the sentinel.

    On the ``asks`` kind the outcome also carries the re-park sentinel over EVERY still-open id of the
    step (its caller subset in ``caller_interaction_ids``), so a caller passing the step up hands the
    sentinel itself back; the user-only ``parked`` kind returns the incoming sentinel unchanged.
    """
    if sentinel.caller_interaction_ids:
        asks = await _entries_for(store, settings, candidates, sentinel.caller_interaction_ids)
        open_sentinel = await _open_step_sentinel(
            store, settings, sentinel.interaction_ids, sentinel.caller_interaction_ids
        )
        return "asks", None, asks, open_sentinel
    return "parked", None, [], sentinel


async def _normalise_buffered(
    store: InteractionStore | None,
    settings: Any,
    candidates: SubjectCandidates | None,
    buffered: ResumeBuffered,
) -> tuple[_Kind, Any, list[ParkedEntry], SuspendedInteraction | None]:
    """A ``ResumeBuffered`` → ``asks`` for the remaining caller ids, else ``parked`` over the user ids.

    Both kinds carry the re-park sentinel over the still-open ids of the step: the ``asks`` kind over
    ALL remaining ids (its caller subset split out), the ``parked`` kind over the user ids alone.
    """
    index = await _parked_index(store, settings, candidates)
    caller_ids = [iid for iid in buffered.remaining_ids if index.get(iid, {}).get("to") == "caller"]
    user_ids = [iid for iid in buffered.remaining_ids if index.get(iid, {}).get("to") == "user"]
    if caller_ids:
        asks = [ParkedEntry(**index[iid]) for iid in caller_ids if iid in index]
        open_sentinel = await _open_step_sentinel(store, settings, buffered.remaining_ids, caller_ids)
        return "asks", None, asks, open_sentinel
    sentinel = await _open_step_sentinel(store, settings, user_ids, [])
    return "parked", None, [], sentinel


async def _open_step_sentinel(
    store: InteractionStore | None, settings: Any, open_ids: list[str], caller_ids: list[str]
) -> SuspendedInteraction | None:
    """Build the re-park sentinel over the still-open ids of one super-step (user + caller).

    ``interaction_ids`` is every still-open id; ``caller_interaction_ids`` is the ``to="caller"``
    subset; ``resume_owner`` is the continuation tool stored on those interactions, which one step
    shares. Differing resume owners across the open ids is a corrupt step and raises rather than
    picking one. ``None`` when nothing remains.
    """
    if not open_ids or store is None:
        return None
    owners: set[str] = set()
    async with client_ctx(RedisClient, settings.redis) as r:
        for iid in open_ids:
            state = await store.get_state(r, iid)
            if state is not None and state.request.continuation_tool is not None:
                owners.add(state.request.continuation_tool)
    if len(owners) > 1:
        raise RuntimeError(
            f"a suspended super-step's interactions carry differing resume owners {sorted(owners)}; "
            "one step has one resume owner"
        )
    return SuspendedInteraction(
        interaction_id=open_ids[0],
        interaction_ids=list(open_ids),
        caller_interaction_ids=list(caller_ids),
        resume_owner=next(iter(owners), None),
    )


async def _entries_for(
    store: InteractionStore | None, settings: Any, candidates: SubjectCandidates | None, ids: list[str]
) -> list[ParkedEntry]:
    """The full parked entries of ``ids``, read back off the run's subject list after a park."""
    index = await _parked_index(store, settings, candidates)
    return [ParkedEntry(**index[iid]) for iid in ids if iid in index]


# --- the shared park answer + start normalisation --------------------------------------


def park_answer(outcome: VisitOutcome) -> Any:
    """The ONE park-answer shape a direct door hands back for a visit outcome.

    A plain ``result`` is the tool's own value; an ``asks`` outcome is the caller ask entries
    the run parked; a ``parked`` outcome is the full suspended sentinel dump; ``none`` is null.
    Wrapped secrets are NOT revealed here — the sync door reveals them on the ``result`` kind
    itself and every recorder masks its own copy.
    """
    if outcome.kind == "result":
        return outcome.result
    if outcome.kind == "asks":
        return {"asks": [entry.model_dump(mode="json") for entry in outcome.asks]}
    if outcome.kind == "parked":
        return outcome.suspended.model_dump(mode="json") if outcome.suspended is not None else None
    return None


async def normalise_started(value: Any) -> VisitOutcome:
    """Classify a raw start return into a ``started`` :class:`VisitOutcome` over the ambient subject.

    The same normalisation :func:`visit` applies to what ``start`` returned, exposed for a door
    that already ran its start inside its OWN visit and only needs the return classified into
    caller asks / a re-park / a final result. Resolves the store, settings and ambient subject
    candidates exactly as :func:`visit` does before it starts.
    """
    settings = interactions_settings() if interactions_store_configured() else None
    store = InteractionStore(settings.key_prefix) if settings is not None else None
    ctx = current_state_context()
    candidates = ctx.candidates if ctx is not None else None
    kind, result, asks, suspended = await _normalise(store, settings, candidates, value)
    return VisitOutcome(action="started", cancelled=[], kind=kind, result=result, asks=asks, suspended=suspended)


# --- the generic facade wrappers -------------------------------------------------


async def list_parked() -> list[ParkedEntry]:
    """Every parked interaction on the current run's subject — the full parked entries."""
    return await list_parked_for(current_state_context())


async def list_parked_for(context: StateContext | None) -> list[ParkedEntry]:
    """Every parked interaction on ``context``'s subject — the full parked entries.

    A door fetches this over its OWN :class:`~tai42_contract.states.StateContext` once and feeds the
    list to :func:`~tai42_kit.utils.door_contract.evaluate_door_contract` as ``$parked``, so the
    contract evaluation stays a pure function of an injected list. ``None`` context (or an
    unconfigured store) has no subject to union over and returns an empty list.
    """
    if context is None or not interactions_store_configured():
        return []
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        entries = await store.list_parked_for(r, context.candidates)
    return [ParkedEntry(**entry) for entry in entries]


async def resume_parked(interaction_id: str, payload: Any = _TAKE) -> VisitOutcome:
    """Resume caller ask ``interaction_id`` with ``payload``, or TAKE its waiting outcome when omitted."""
    item: ResumeItem | TakeItem = (
        TakeItem(id=interaction_id) if payload is _TAKE else ResumeItem(id=interaction_id, payload=payload)
    )
    return await visit(
        target_name="resume_parked", cancel=[], resume=[item], start=None, extras={}, receives_outcome=True
    )


async def cancel_parked(ids: list[str]) -> VisitOutcome:
    """Whole-chain kill every parked interaction named in ``ids`` on the current run's subject."""
    return await visit(
        target_name="cancel_parked", cancel=list(ids), resume=[], start=None, extras={}, receives_outcome=True
    )


# --- small helpers ---------------------------------------------------------------------


def _now() -> datetime:
    """The current UTC time an answered record is stamped with."""
    return datetime.now(UTC)


def _reply_ttl(request: InteractionRequest) -> int:
    """Short reply-key TTL ≈ the remaining timeout budget, so a late claim expires rather than resurrects."""
    remaining = int((request.timeout_at - _now()).total_seconds())
    return max(1, remaining)


__all__ = [
    "cancel_parked",
    "list_parked",
    "normalise_started",
    "park_answer",
    "resume_parked",
    "visit",
]
