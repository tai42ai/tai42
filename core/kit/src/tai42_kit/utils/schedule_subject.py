"""The reserved job kwargs a scheduled fire carries its subject and its door-layer state
binding under, and the worker pop that turns them back into a
:class:`~tai42_contract.states.StateSubject` context and a deposited
:class:`~tai42_contract.tools.ToolInvocation` state binding.

A schedule fire is anonymous/system — the worker sees only the job kwargs, so "this is a
schedule, keyed on this subject, applying this binding" is stamped where it is KNOWN (at
creation, by the backend ``schedule_task`` wrapper) and read back where it is USED (at the
fire, by the worker). Homed in kit beside
:data:`tai42_kit.utils.worker_secret_capability.WORKER_SECRET_CAPABILITY_ARG` because the
execution backends below the skeleton stamp and pop these and never import tai42-skeleton;
the skeleton's write chokepoint reads the deposited context, and its dispatch chokepoint
reads the deposited binding, not these args.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tai42_contract.states import StateBinding, StateContext, StateSubject, SubjectCandidates
from tai42_contract.tools import (
    ToolInvocation,
    current_tool_invocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)

from tai42_kit.utils.state_context import state_context

# The reserved job kwarg a scheduled fire carries its subject under. Stamped server-side
# by the backend ``schedule_task`` wrapper (only when ``scheduled=True``), so a submit
# wrapper stamps nothing and a caller can never forge it; namespaced under the
# ``backend_`` dispatch-kwarg convention so it cannot collide with a tool parameter.
SCHEDULE_SUBJECT_ARG = "backend_schedule_subject"

# The reserved job kwarg a scheduled fire carries its door-layer state binding under.
# Stamped server-side by the backend ``schedule_task`` wrapper (only when ``scheduled=True``)
# under the same ``backend_`` convention. UNLIKE the subject — which also stays a live
# ``subject`` kwarg a state tool reads — the raw request key is POPPED at the stamp, so the
# binding never reaches the base tool (tools stay pure).
SCHEDULE_STATE_BINDING_ARG = "backend_schedule_state_binding"


def pop_schedule_subject(kwargs: dict[str, Any]) -> StateSubject | None:
    """Strip :data:`SCHEDULE_SUBJECT_ARG` from ``kwargs`` and parse it into a
    :class:`StateSubject`, or return ``None`` when the job carries none.

    Mutates ``kwargs`` in place so the popped dispatch kwarg never reaches the tool,
    the same shape the worker's secret-capability pop uses. A malformed value raises
    loudly — a stamped-but-unparseable subject is a bug, never a silent skip."""
    raw = kwargs.pop(SCHEDULE_SUBJECT_ARG, None)
    if raw is None:
        return None
    if isinstance(raw, StateSubject):
        return raw
    return StateSubject.model_validate(raw)


def pop_schedule_state_binding(kwargs: dict[str, Any]) -> StateBinding | None:
    """Strip :data:`SCHEDULE_STATE_BINDING_ARG` from ``kwargs`` and parse it into a
    :class:`StateBinding`, or return ``None`` when the job carries none.

    Mutates ``kwargs`` in place so the reserved kwarg never reaches the tool. A
    malformed value raises loudly — a stamped-but-unparseable binding is a bug, never a
    silent skip."""
    raw = kwargs.pop(SCHEDULE_STATE_BINDING_ARG, None)
    if raw is None:
        return None
    if isinstance(raw, StateBinding):
        return raw
    return StateBinding.model_validate(raw)


@contextmanager
def schedule_state_context(kwargs: dict[str, Any]) -> Iterator[None]:
    """The ``schedule``-door state context a worker fire runs the tool inside — the one
    seam all three backend workers share so a scheduled write is keyed, attributed, and
    bound identically on every backend.

    Pops :data:`SCHEDULE_SUBJECT_ARG` and :data:`SCHEDULE_STATE_BINDING_ARG` from ``kwargs``
    (in place, so neither reaches the tool). When the job carried a subject, enters a
    :func:`state_context` for a ``schedule`` door keyed on it (``actor`` is ``None`` — a
    schedule fire is anonymous/system); with none it runs context-free (its ``api`` door is
    stamped at the write chokepoint). INDEPENDENTLY — a binding with no schedule subject
    still deposits — when the job carried a binding, it is deposited onto
    :attr:`ToolInvocation.state_binding` so the dispatch chokepoint carries it forward and
    merges it exactly like every other door's binding."""
    subject = pop_schedule_subject(kwargs)
    binding = pop_schedule_state_binding(kwargs)
    token = None
    if binding is not None:
        # A carrier deposit: the dispatch chokepoint re-deposits with the real invoked-tool
        # name and reads only ``state_binding`` forward, so the name here is a placeholder
        # (the current one if a nested fire already deposited, else the schedule door).
        prior = current_tool_invocation()
        tool_name = prior.tool_name if prior is not None else "schedule"
        token = set_current_tool_invocation(ToolInvocation(tool_name=tool_name, state_binding=binding))
    try:
        if subject is None:
            yield
        else:
            with state_context(
                StateContext(
                    door="schedule",
                    candidates=SubjectCandidates(
                        target_kind=subject.target_kind,
                        target_name=subject.target_name,
                        by_kind={subject.kind: subject.key},
                    ),
                    actor=None,
                )
            ):
                yield
    finally:
        if token is not None:
            reset_current_tool_invocation(token)
