"""The reserved job kwarg a scheduled fire carries its subject under, and the worker
pop that turns it back into a :class:`~tai42_contract.states.StateSubject`.

A schedule fire is anonymous/system — the worker sees only the job kwargs, so "this is
a schedule, keyed on this subject" is stamped where it is KNOWN (at creation, by the
backend ``schedule_task`` wrapper) and read back where it is USED (at the fire, by the
worker). Homed in kit beside :data:`tai42_kit.utils.worker_secret_capability.WORKER_SECRET_CAPABILITY_ARG`
because the execution backends below the skeleton stamp and pop it and never import
tai42-skeleton; the skeleton's write chokepoint reads the deposited context, not this arg.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any

from tai42_contract.states import StateContext, StateSubject, SubjectCandidates

from tai42_kit.utils.state_context import state_context

# The reserved job kwarg a scheduled fire carries its subject under. Stamped server-side
# by the backend ``schedule_task`` wrapper (only when ``scheduled=True``), so a submit
# wrapper stamps nothing and a caller can never forge it; namespaced under the
# ``backend_`` dispatch-kwarg convention so it cannot collide with a tool parameter.
SCHEDULE_SUBJECT_ARG = "backend_schedule_subject"


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


def schedule_state_context(kwargs: dict[str, Any]) -> AbstractContextManager[None]:
    """The ``schedule``-door state context a worker fire runs the tool inside — the one
    seam all three backend workers share so a scheduled write is keyed and attributed
    identically on every backend.

    Pops :data:`SCHEDULE_SUBJECT_ARG` from ``kwargs`` (in place, so it never reaches the
    tool) and, when the job carried a subject, returns a :func:`state_context` for a
    ``schedule`` door keyed on that subject (``actor`` is ``None`` — a schedule fire is
    anonymous/system). With no stamped subject it returns a :class:`nullcontext`, so a
    plain background run stays context-free (its ``api`` door is stamped at the write
    chokepoint)."""
    subject = pop_schedule_subject(kwargs)
    if subject is None:
        return nullcontext()
    return state_context(
        StateContext(
            door="schedule",
            candidates=SubjectCandidates(
                target_kind=subject.target_kind, target_name=subject.target_name, by_kind={subject.kind: subject.key}
            ),
            actor=None,
        )
    )
