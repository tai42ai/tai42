"""The parked-run API — what a plugin calls to drive a run's parked interactions without ``run_tool``.

:func:`list_parked` / :func:`list_parked_for` read the parked entries on a subject;
:func:`resume_parked` resumes a caller ask or takes its waiting outcome, and :func:`cancel_parked`
kills the runs of named entries, both through the shared :func:`~tai42_skeleton.interactions.visit.visit`;
:func:`settle_visit_claim` reads which ids of a resume or take claim are no longer open.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.interactions import ParkedEntry, ParkedEntryGoneError, ResumeItem, TakeItem, VisitAct, VisitOutcome
from tai42_contract.states import StateContext
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.interactions.visit import visit
from tai42_skeleton.states.context import current_state_context

# The sentinel distinguishing ``resume_parked``'s "no payload given → take" from a real ``None``
# answer payload. It never appears in a stored payload.
_TAKE = object()


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
    store = InteractionStore(interactions_settings().key_prefix)
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
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
    """Kill the whole run of every parked interaction named in ``ids`` on the current run's subject."""
    return await visit(
        target_name="cancel_parked", cancel=list(ids), resume=[], start=None, extras={}, receives_outcome=True
    )


async def settle_visit_claim(act: VisitAct) -> tuple[str, ...]:
    """The ids of ``act``'s resume or take claim that are no longer open in the interactions store.

    A resume entry is open while its state is ``pending``; an ``answered`` or missing state is not.
    A take entry is open while its outcome still waits. The read cannot tell which door resolved an
    entry no longer open, so a returned id never proves this caller's claim committed; an empty
    result proves no claim on these ids did. A store fault raises.
    """
    if act.action not in ("resume", "take"):
        raise ValueError(f"only a resume or take claim can be settled, got {act.action!r}")
    if not interactions_store_configured():
        raise ParkedEntryGoneError("no interactions store is configured")
    store = InteractionStore(interactions_settings().key_prefix)
    gone: list[str] = []
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        for iid in act.interaction_ids:
            if act.action == "resume":
                state = await store.get_state(r, iid)
                still_open = state is not None and state.status == "pending"
            else:
                still_open = await store.outcome_waiting(r, iid)
            if not still_open:
                gone.append(iid)
    return tuple(gone)


__all__ = ["cancel_parked", "list_parked", "list_parked_for", "resume_parked", "settle_visit_claim"]
