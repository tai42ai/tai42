"""A parked run's per-run completion id and the deferred door binding applied at its terminal.

Every rung of the continuation delivery ladder keys on the run's completion id, and the deferred
binding apply keys on the same id, so a redelivery of the same terminal lands once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from tai42_contract.states import StateBinding

__all__ = ["apply_deferred_binding", "run_completion_id"]


def run_completion_id(run_delivery_id: str) -> str:
    """The per-run delivery id every ladder rung keys on — a uuid5 over the run's delivery identity.

    A run has exactly one terminal, so keying on the RUN (not on whichever interaction's resume
    reached the terminal) makes every sibling / re-park / nested ask of the run resolve to the SAME
    id: an address fire dedupes, a waiting outcome writes once, a redelivery lands once.
    """
    return str(uuid5(NAMESPACE_URL, f"tai42:park-delivery:{run_delivery_id}"))


async def apply_deferred_binding(
    *,
    deferred_binding: StateBinding | None,
    run_input: Mapping[str, Any] | None,
    door_id: str | None,
    output: Any,
    run_delivery_id: str | None,
) -> None:
    """Apply a parked run's DEFERRED door binding UPDATES at its real terminal, once per run.

    The door that STARTED the run dropped its binding updates on the park (a pause is not a clean
    output); they are applied HERE, over the run's real terminal ``output``, exactly as the live
    dispatch would have. Keyed on the run's completion id so an at-least-once redelivery
    re-driving the same terminal replays the same ops idempotently. A run that carried no binding
    (``deferred_binding`` None — a bindingless door) is a no-op; a stored binding with no run input
    or door id is a torn record and raises.
    """
    if deferred_binding is None:
        return
    if run_delivery_id is None:
        raise RuntimeError(
            "a parked run carries a deferred binding but no run_delivery_id to key its terminal apply on"
        )
    if run_input is None or door_id is None:
        raise RuntimeError("a parked run's deferred binding is missing its run input or door id")
    # The concrete process app the binding runtime applies through (``app.states``/``app.storage``).
    # Function-local: a module-level edge into the app singleton would couple the detached-fire
    # module to the whole server build at import.
    from tai42_skeleton.app.instance import build_app
    from tai42_skeleton.tools.state_binding import apply_binding_updates

    await apply_binding_updates(
        build_app(),
        deferred_binding,
        dict(run_input),
        output,
        door_id,
        idempotency_scope=run_completion_id(run_delivery_id),
    )
