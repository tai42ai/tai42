"""The platform's resume- and delivery-authorization predicates and the redelivery horizon.

A driver's continuation face and a door's delivery-address tool are registered tools, dispatchable
by name at the run-tool door and the MCP edge, yet each may produce a run's outcome ONLY inside the
platform's own resume/delivery of that run. These predicates are the gate: the face/tool asserts
the platform's run-authorization or delivery-fire context before it acts, keyed to the thing it is
about to resume or deliver — never a blanket "hidden tools refuse outside doors" rule. A
cross-driver chain re-entry names a chained call rather than an interaction; it is keyed to the
chain lineage the parked interaction recorded, which the platform deposits around every drive of it.
"""

from __future__ import annotations

from tai42_contract.interactions import (
    ParkDeliveryUnauthorizedError,
    ParkResumeUnauthorizedError,
    is_chained_park_key,
)
from tai42_contract.tools import get_run_delivery_id
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.runs.chokepoint import get_delivery_fire, get_resume_lineage, get_resume_origin


async def assert_resume_authorized(interaction_id: str) -> None:
    """Raise unless the current run is the platform's own drive of ``interaction_id``'s run.

    A platform drive (a resume, a kill teardown, a give-up) must be on the stack: the ambient resume
    origin is set. Then exactly one of three facts authorises the call:

    * the origin EQUALS ``interaction_id`` — the drive of that very interaction;
    * ``interaction_id`` is an interaction whose stored ``run_delivery_id`` equals the ambient one —
      another interaction of the same run;
    * ``interaction_id`` is a chain key (:func:`~tai42_contract.interactions.is_chained_park_key`) in
      the ambient resume lineage — a chained call the driven interaction's run is nested under, which
      its park recorded and the drive deposited. A chain key holds no interaction state, so the
      lineage is its only authority: the driven run re-entering a waiting caller across drivers is
      admitted at any nesting depth, on every door that drives the interaction.

    A ``None`` origin — an external run-tool / MCP caller naming the face — is refused, as is an
    interaction of another run and a chain key outside the driven interaction's lineage (a resume
    origin that leaked into a deeper run). Never a mere presence test.
    """
    origin = get_resume_origin()
    if origin is None:
        raise ParkResumeUnauthorizedError(
            f"resume of interaction {interaction_id!r} refused: no platform resume drive is on the stack "
            "(this face may only produce an outcome inside the platform's own resume of its run)"
        )
    if origin == interaction_id:
        return
    if is_chained_park_key(interaction_id):
        if interaction_id in get_resume_lineage():
            return
        raise ParkResumeUnauthorizedError(
            f"resume of chained call {interaction_id!r} refused: it is not a chained call the resumed "
            "interaction is nested under"
        )
    ambient_run_delivery_id = get_run_delivery_id()
    if ambient_run_delivery_id is not None and ambient_run_delivery_id == await _stored_run_delivery_id(interaction_id):
        # The origin names a different interaction of the SAME run: the shared run delivery
        # identity authorises it.
        return
    raise ParkResumeUnauthorizedError(
        f"resume of interaction {interaction_id!r} refused: the ambient resume drive belongs to a different run"
    )


async def _stored_run_delivery_id(interaction_id: str) -> str | None:
    """The interaction's stored ``run_delivery_id``, or ``None`` when the store is off or its state is gone."""
    if not interactions_store_configured():
        return None
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        state = await store.get_state(r, interaction_id)
    return state.request.run_delivery_id if state is not None else None


def assert_delivery_authorized(completion_id: str | None) -> None:
    """Raise unless the platform's delivery ladder is firing this address tool for ``completion_id``.

    Passes iff the ambient delivery-fire context is set AND equals ``completion_id``. A ``None`` id
    (no legitimate fire is stamped without one) and any call outside the ladder's fire never pass.
    """
    if completion_id is None or get_delivery_fire() != completion_id:
        raise ParkDeliveryUnauthorizedError(
            "delivery refused: this address tool delivers only inside the platform's own delivery fire "
            f"for its completion (completion_id={completion_id!r})"
        )


def redelivery_horizon_seconds() -> int:
    """The platform's redelivery retention horizon in seconds — the idle retention TTL.

    The continuation-due record ages out at it and the reaper caps its backoff at it, so no
    redelivery fires past it. A resuming driver derives its resolution-record retention from it.
    """
    return interactions_settings().idle_ttl_seconds
