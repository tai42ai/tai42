"""The schedule door: one function every scheduled fire — worker or run-once — drives through.

:func:`fire_schedule_door` is the ONE schedule-door mechanism. It takes RESOLVED inputs (a subject,
a door-layer state binding, a door contract, and whether a live receiver awaits the outcome),
deposits the ``schedule`` subject state context, fetches the run's parked interactions over it,
evaluates the door contract with ``$parked`` bound, and drives the shared
:func:`tai42_app.interactions.visit` — which applies the binding around the started tool alone, so a
``resume_expr`` resume never runs its continuation under the target's binding.

:func:`backend_fire` is the seam each backend worker enters to fire a dequeued job: it pops the
reserved ``backend_schedule_*`` job kwargs into objects and drives the door (binding the stamped
firing identity when one rode the job), or, for a plain job carrying no door signal, runs ``run_tool``
with no door context.

Homed in kit's backend package so a backend plugin reaches it without importing the skeleton; it
drives the skeleton's ``visit`` only through the bound ``tai42_app`` handle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions.door_contract import ParkableDoorMixin

from tai42_kit.interactions import DOOR_START_DEFAULT, evaluate_door_contract, parked_entries_for_jq
from tai42_kit.utils.schedule_subject import (
    pop_schedule_contract,
    pop_schedule_execution_identity,
    pop_schedule_state_binding,
    pop_schedule_subject,
    schedule_subject_context,
)
from tai42_kit.utils.state_context import current_state_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from tai42_contract.interactions import VisitOutcome
    from tai42_contract.states import StateBinding, StateSubject


async def fire_schedule_door(
    dispatch_name: str,
    arguments: dict[str, Any],
    *,
    subject: StateSubject | None,
    state_binding: StateBinding | None,
    contract: ParkableDoorMixin | None,
    receives_outcome: bool,
) -> VisitOutcome:
    """Drive one schedule fire (a recurring worker fire, or the run-once create dispatch) through ``visit``.

    Deposits the ``schedule`` subject context, fetches the run's parked interactions over it, evaluates
    ``contract`` (an absent contract is the empty one — start the base ``arguments``, cancel/resume
    nothing) with ``$parked`` bound, and drives ``visit`` with ``state_binding`` applied around the
    start alone. ``receives_outcome`` is ``True`` for the run-once create (a live HTTP receiver takes
    the outcome inline) and ``False`` for a recurring worker fire (receiver-less — a park is
    subject-tracked and a ``to="caller"`` resume fires the run's own delivery).
    """
    with schedule_subject_context(subject):
        context = current_state_context()
        parked = await tai42_app.interactions.list_parked_for(context)
        outcome = await evaluate_door_contract(
            contract or ParkableDoorMixin(),
            arguments,
            parked_entries_for_jq(parked),
        )
        start = _schedule_start(dispatch_name, arguments, outcome)
        return await tai42_app.interactions.visit(
            target_name=dispatch_name,
            cancel=outcome.cancel,
            resume=outcome.resume,
            start=start,
            extras=outcome.extras,
            state_binding=state_binding,
            receives_outcome=receives_outcome,
        )


def _schedule_start(
    dispatch_name: str, arguments: dict[str, Any], outcome: Any
) -> Callable[[Mapping[str, Any]], Awaitable[Any]] | None:
    """The ``visit`` start callable for a schedule fire, or ``None`` when the contract starts nothing.

    ``None`` means a declared ``start_expr`` yielded null — nothing is started (the run's parked
    interactions may still be cancelled or resumed). With no ``start_expr`` the fire dispatches its
    stored ``arguments`` unchanged; a ``start_expr`` object replaces them. The ``extras`` ``visit``
    hands the callable reach the started run's dispatch.
    """
    if outcome.start is None:
        return None
    kwargs = arguments if outcome.start is DOOR_START_DEFAULT else outcome.start

    async def _start(extras: Mapping[str, Any]) -> Any:
        return await tai42_app.tools.run_tool(dispatch_name, kwargs, offload_sync=True, extras=extras)

    return _start


async def backend_fire(tool_name: str, kwargs: dict[str, Any]) -> Any:
    """The one seam a backend worker enters to fire a scheduled/forwarded job through the schedule door.

    Pops the reserved ``backend_schedule_*`` kwargs (in place, so none reaches the tool) and:

    * a job carrying a stamped firing identity (a contract-bearing schedule, or a background task tool
      that forwarded the ambient subject + identity) → bind that identity and drive the door
      receiver-less, returning the run's result / re-park sentinel;
    * a job carrying a forwarded subject/binding but no identity → drive the door under the ambient
      identity, receiver-less;
    * a plain job with no door signal → ``run_tool`` with no door context, returning its result.

    The worker's detached-run marker and secret-capability bind wrap this call; they are not this
    seam's concern.
    """
    subject = pop_schedule_subject(kwargs)
    state_binding = pop_schedule_state_binding(kwargs)
    identity = pop_schedule_execution_identity(kwargs)
    contract = pop_schedule_contract(kwargs)

    if subject is None and state_binding is None and identity is None and contract is None:
        return await tai42_app.tools.run_tool(tool_name, kwargs, offload_sync=True)

    if identity is not None:
        user_id, fingerprint = identity
        async with tai42_app.interactions.bound_execution_identity_for_fire(user_id, fingerprint):
            outcome = await fire_schedule_door(
                tool_name,
                kwargs,
                subject=subject,
                state_binding=state_binding,
                contract=contract,
                receives_outcome=False,
            )
    else:
        outcome = await fire_schedule_door(
            tool_name,
            kwargs,
            subject=subject,
            state_binding=state_binding,
            contract=contract,
            receives_outcome=False,
        )
    return visit_return(outcome)


def visit_return(outcome: VisitOutcome) -> Any:
    """The worker-job return for a door-driven fire: the result, else the re-park sentinel, else ``None``.

    A receiver-less fire returns the run's terminal value (``kind="result"``); a park —
    caller asks (``kind="asks"``) or user-only (``kind="parked"``) — returns the re-park sentinel so
    the recorded job outcome is the ``SuspendedInteraction`` a parking tool returns; a run
    that started nothing returns ``None``.
    """
    if outcome.kind == "result":
        return outcome.result
    if outcome.kind in ("asks", "parked"):
        return outcome.suspended
    return None


__all__ = ["backend_fire", "fire_schedule_door", "visit_return"]
