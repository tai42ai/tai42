"""The async park's resume binding and re-park notice.

Capture the resume continuation + execution identity + fingerprint +
state-context + bound thread when a park is raised, and notify a chained caller
when the run it addresses re-parks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    CallerAskLanding,
    DeliveryAddress,
    RunTerminalFailed,
    caller_ask_no_landing_outcome,
    chained_lineage,
    current_caller_ask_landing,
    get_chained_resume,
    get_park_completion,
    get_resume_continuation_tool,
)
from tai42_contract.presets import PresetParkStages
from tai42_contract.states import StateBinding, StateContext
from tai42_contract.tools import get_run_delivery, get_run_delivery_id
from tai42_kit.interactions.park_adoption import repark_notice

from tai42_skeleton.authz import execution_identity
from tai42_skeleton.interactions.continuation import _current_state_context_for_park
from tai42_skeleton.runs.chokepoint import get_resume_lineage
from tai42_skeleton.tools.stage_frames import capture_park_stages
from tai42_skeleton.tools.state_binding import current_deferred_binding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AsyncParkBinding:
    """The fields an async park's resume binding carries.

    The resume continuation tool, the execution identity + key fingerprint to
    rebind it as, the parking turn's state context, and the bound conversation
    thread (all ``None`` for a sync ask).
    """

    continuation_tool: str | None = None
    continuation_identity: str | None = None
    continuation_fingerprint: str | None = None
    continuation_state_context: StateContext | None = None
    park_thread_id: str | None = None
    # The RUN's delivery identity and out-of-band address, read from the ambient
    # run-delivery context the outermost ``tool_call_frame`` bound. The run_delivery_id
    # is always present for a parkable run (the persist raises without one); the address
    # is ``None`` for a receiver-less run. Both are stored verbatim on the request.
    run_delivery_id: str | None = None
    delivery: DeliveryAddress | None = None
    # The caller-ask landing the door declared around the run, captured so the resume re-establishes
    # it. ``None`` when no door declared one.
    caller_ask_landing: CallerAskLanding | None = None
    # The door's MERGED state binding, the run input it saw, and the door id — captured from the
    # outermost dispatch so the deferred binding UPDATES apply once at the run's real terminal
    # instead of being dropped on the pause. All ``None`` when the run carried no state binding.
    deferred_binding: StateBinding | None = None
    run_input: dict[str, Any] | None = None
    door_id: str | None = None
    # The chain keys of the chained calls the parking run is nested under, outermost first: the
    # lineage of the interaction the platform is driving (a re-park inherits it), then every key of
    # the ambient chained dispatches not already in it. Empty for a run nested under no chained call.
    chain_keys: tuple[str, ...] = ()
    # The output stages of the preset dispatches the park sits under, by where each preset's result
    # reappears after the resume. ``None`` when the park sits under no preset stage.
    deferred_output_stages: PresetParkStages | None = None


def resolve_async_continuation(to: Literal["user", "caller"] = "user") -> AsyncParkBinding:
    """Resolve the async park's resume binding, raising loudly when a driver/identity is missing.

    Captures the resume tool + execution identity + fingerprint + state-context +
    bound thread, and the RUN's delivery identity + address. An async ask with no bound
    driver, no identity to rebind it as, or (a platform bug) no ambient run-delivery
    identity is a failure that must fail loudly, never persist a question no answer/expiry
    could resume or deliver. A ``caller`` ask additionally requires an ambient state context
    (its outcome is subject-tracked back through the parking run's subject) AND a door that
    declares a landing for it: a caller ask about to park where the door declared NO landing
    (no reply/resume path) would park nowhere, so the run FAILS loudly (:class:`RunTerminalFailed`)
    before any state is written — it is never a tool-level error the model could route around.
    """
    landing = current_caller_ask_landing()
    if to == "caller" and landing is not None and not landing.can_land:
        raise RunTerminalFailed(caller_ask_no_landing_outcome(landing.label))
    continuation_tool = get_resume_continuation_tool()
    if continuation_tool is None:
        raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
    identity = execution_identity.get_execution_identity()
    if identity is None or identity.user_id is None:
        raise RuntimeError("async ask requires a bound execution identity to rebind the continuation as")
    # Stashed alongside the identity so the answer/expiry path rebinds the continuation
    # under the SAME fire authority the ask ran under; gate-off fires carry no
    # fingerprint, recorded as "" (the bind ignores it there).
    continuation_fingerprint = identity.execution_key_fingerprint or ""
    # The parking turn's ambient state context, captured here inside that turn so the
    # resume re-deposits the original door's subject + write provenance.
    continuation_state_context = _current_state_context_for_park()
    if to == "caller" and continuation_state_context is None:
        # A caller ask's outcome is handed back through the parking run's subject, read
        # from the ambient state context; with none there is no subject to track it on.
        raise RuntimeError("a caller ask (to='caller') requires an ambient state context")
    # The run's conversation thread (off its delivery address), so this park joins its thread's
    # reverse index and a thread delete can cancel it. ``None`` outside a conversation run.
    park_thread_id = bound_park_thread_id()
    # The RUN's delivery identity + address, bound once at the outermost run start on the
    # ambient run-delivery context. A parkable run ALWAYS started through the frame that
    # mints it, so a missing id is a platform bug — raise, never persist a park the
    # delivery chokepoint could not key.
    run_delivery_id = get_run_delivery_id()
    if run_delivery_id is None:
        raise RuntimeError("async ask parked with no ambient run_delivery_id (the run started outside a call frame)")
    delivery = get_run_delivery()
    # The merged door binding the outermost dispatch deposited (after its injections ran), so the
    # deferred UPDATES apply once at the run's real terminal. A copy of the run input is taken so a
    # later mutation of the live arguments never reaches the stored snapshot.
    deferred = current_deferred_binding()
    chain_keys = park_chain_keys()
    return AsyncParkBinding(
        chain_keys=chain_keys,
        deferred_output_stages=capture_park_stages(chain_keys),
        continuation_tool=continuation_tool,
        continuation_identity=identity.user_id,
        continuation_fingerprint=continuation_fingerprint,
        continuation_state_context=continuation_state_context,
        park_thread_id=park_thread_id,
        run_delivery_id=run_delivery_id,
        delivery=delivery,
        caller_ask_landing=landing,
        deferred_binding=deferred.binding if deferred is not None else None,
        run_input=dict(deferred.run_input) if deferred is not None else None,
        door_id=deferred.door_id if deferred is not None else None,
    )


def park_chain_keys() -> tuple[str, ...]:
    """The chain keys a park records: the lineage it inherits, then the chained calls this run crossed, outermost first.

    Inside a platform drive the lineage of the interaction being driven comes first (a re-park is
    nested under every chained call its resumed run was), followed by each key of the chained
    calls whose run face this context crossed (:func:`~tai42_contract.interactions.chained_lineage`)
    not already in it, in order — so a driver re-binding its captured routing adds nothing and a
    new nested chained call is appended. Outside a drive it is the crossed chained lineage alone;
    a park the dispatcher raises itself under its own routing records nothing of that routing.
    """
    inherited = get_resume_lineage()
    return inherited + tuple(key for key in chained_lineage() if key not in inherited)


def bound_park_thread_id() -> str | None:
    """The conversation thread this async park belongs to, or ``None`` when none is bound.

    Read off the run's delivery address: the one the run's outermost frame captured when a run
    delivery is bound — the platform re-establishes it around every out-of-band drive (an answer,
    an expiry, a reaper redelivery), so a re-park there keeps its thread for both route kinds —
    else the completion address the door bound. A background tool run or a direct ask binds no
    thread, so its park is not thread-indexed and a thread delete does not reach it.
    """
    run_address = get_run_delivery()
    if run_address is not None:
        return run_address.thread_id
    return get_park_completion().thread_id


async def notify_repark(expiry_at: datetime | None, *, interaction_id: str) -> None:
    """Tell a chained caller that the run it waits on just re-parked on a new ask.

    A caller whose own suspension horizon was inherited from that run can then refresh it.

    Fired only for a RE-park of a run a chained caller waits on: a chain routing is bound and its
    chain key is in the lineage of the park being driven (:func:`get_resume_lineage`) — the
    caller parked on that key or released it at the run's first park. A first park, and a park
    the dispatcher raises itself under its own routing, notify nothing, so no delivery tool ever
    sees a fire it has no horizon to answer.
    BEST-EFFORT by construction: the notice refreshes a horizon, it never
    carries an answer, so a failing notifier is logged and swallowed rather than turning a
    successfully persisted park into a failed ``ask``. The cost of a lost notice is a
    caller whose horizon stays at the previous ask's deadline.
    """
    routing = get_chained_resume()
    notice = repark_notice(expiry_at) if routing is not None and routing.chain_key in get_resume_lineage() else None
    if notice is None:
        return
    tool, payload = notice
    try:
        await tai42_app.tools.run_tool(tool, payload)
    except Exception:
        logger.warning(
            "ask: the re-park horizon notice to %r failed for interaction %s; the chained caller "
            "keeps its previous horizon",
            tool,
            interaction_id,
            exc_info=True,
        )
