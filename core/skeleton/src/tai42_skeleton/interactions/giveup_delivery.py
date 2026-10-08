"""Delivering the outcome of a PERMANENTLY abandoned async park.

The expiry reaper drops a continuation-due record once it is past the redelivery retention horizon:
no redelivery will ever re-drive that resume. :func:`deliver_park_giveup` then ends the park. The
driver that parked is asked first, through the registered give-up handlers, in the same frame a
receiver-less drive of the park runs in (:func:`~tai42_skeleton.interactions.continuation._continuation_scope`);
when no driver owns a record of the park, the platform delivers the run's single FAILED through the
same delivery ladder a drive's terminal takes.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Final

from tai42_contract.interactions import PARK_COMPLETION_FAILED, InteractionRequest
from tai42_contract.states import SubjectCandidates
from tai42_contract.tools import RunDelivery, run_delivery
from tai42_kit.interactions.park_giveup import fire_park_giveup

from tai42_skeleton.interactions.continuation import _continuation_scope, _deliver_run_outcome, _deliver_terminal
from tai42_skeleton.interactions.store import InteractionStore

logger = logging.getLogger(__name__)

__all__ = ["deliver_park_giveup"]


# The failed-terminal outcome the platform delivers when a resume is permanently abandoned
# (the reaper dropped its due record past the retention horizon). The delivery tool surfaces a
# FAILED status as its uniform notice and ignores this ``result``; a subject-tracked give-up stores
# it as the failure error a subject owner reads.
_RESUME_ABANDONED_OUTCOME: Final[dict[str, Any]] = {"tai42:resume_abandoned": True}


async def deliver_park_giveup(store: InteractionStore, request: InteractionRequest, *, fingerprint: str | None) -> None:
    """Deliver the outcome of a PERMANENTLY abandoned park: its driver's give-up first, else the run's FAILED.

    The reaper dropped the continuation-due record past its retention horizon, so no redelivery will
    ever re-drive this resume. The driver that parked owns the routing its run captured (a chained
    caller, a parent run waiting on it), so the platform first asks the registered give-up handlers
    (:func:`tai42_kit.interactions.park_giveup.fire_park_giveup`) to end the park with the abandoned
    outcome, in the SAME frame a receiver-less drive of this park runs in: the run's stored delivery
    and deferred binding, and the park's continuation identity (``fingerprint`` is the key
    fingerprint the park captured), origin, chain lineage, state context and caller-ask landing. The outermost
    outcome a handler returns is delivered exactly as a drive's would be (a handled answer
    SUCCEEDED, a ``RunFailed`` FAILED, a re-park nothing).

    When no driver owns a record of the park, the platform delivers the run's single FAILED itself —
    fire the run's address, else a ``failed`` waiting outcome on its subject, else drop — keyed by the
    run's ``completion_id`` so it dedupes against any FAILED already delivered. A handler that raises
    is logged, the FAILED is still delivered so the door learns the run failed, and the error is
    re-raised.
    """
    from tai42_skeleton.tools.state_binding import deferred_binding_scope

    candidates = (
        request.continuation_state_context.candidates if request.continuation_state_context is not None else None
    )
    if request.continuation_identity is None:
        raise RuntimeError(f"async interaction {request.interaction_id!r} carries no continuation identity")
    deferred_ctx: AbstractContextManager[Any] = (
        deferred_binding_scope(request.deferred_binding, dict(request.run_input), request.door_id)
        if request.deferred_binding is not None and request.run_input is not None and request.door_id is not None
        else nullcontext()
    )
    delivery_ctx = run_delivery(
        RunDelivery(request.run_delivery_id, request.delivery) if request.run_delivery_id is not None else None
    )
    try:
        with delivery_ctx, deferred_ctx:
            async with _continuation_scope(
                request.continuation_identity,
                fingerprint,
                request.interaction_id,
                request.continuation_state_context,
                request.caller_ask_landing,
                request.chain_keys,
                mark_detached=True,
            ):
                handled = await fire_park_giveup(request.interaction_id, dict(_RESUME_ABANDONED_OUTCOME))
    except Exception:
        logger.error(
            "park give-up for interaction %s failed in its driver's give-up handler; delivering FAILED to the "
            "run's address",
            request.interaction_id,
            exc_info=True,
        )
        await _deliver_abandoned(store, request, candidates)
        raise
    if handled is None:
        await _deliver_abandoned(store, request, candidates)
        return
    await _deliver_run_outcome(
        store,
        interaction_id=request.interaction_id,
        result=handled.result,
        delivery=request.delivery,
        run_delivery_id=request.run_delivery_id,
        candidates=candidates,
        deferred_binding=request.deferred_binding,
        run_input=request.run_input,
        door_id=request.door_id,
    )


async def _deliver_abandoned(
    store: InteractionStore, request: InteractionRequest, candidates: SubjectCandidates | None
) -> None:
    """Deliver the run's single FAILED for an abandoned park no driver ended."""
    await _deliver_terminal(
        store,
        interaction_id=request.interaction_id,
        outcome=_RESUME_ABANDONED_OUTCOME,
        status=PARK_COMPLETION_FAILED,
        delivery=request.delivery,
        run_delivery_id=request.run_delivery_id,
        candidates=candidates,
    )
