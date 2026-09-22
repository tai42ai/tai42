"""The reserved schedule job kwargs and the worker pop that turns them back into state context.

A scheduled fire carries its subject and its door-layer state binding under reserved job
kwargs; the worker pop turns them back into a
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
from contextvars import ContextVar
from typing import Any

from tai42_contract.interactions.door_contract import ParkableDoorMixin
from tai42_contract.states import StateBinding, StateContext, StateSubject, SubjectCandidates

from tai42_kit.utils.state_context import state_context

# The reserved namespace every schedule-door job kwarg lives under. The platform STAMPS each
# one from a validated input and REFUSES any caller-supplied key in this namespace, so a caller can
# never forge one; the ``backend_`` prefix also keeps them from colliding with a tool parameter.
RESERVED_SCHEDULE_PREFIX = "backend_schedule_"

# The one key inside the reserved namespace a caller DOES own: the cadence's name (its revocation
# handle), which the backend ``schedule_task`` branch pops to register the schedule. It rides the
# reserved prefix but is the caller's own input, so it is the single exemption to the refusal rule.
SCHEDULE_NAME_KEY = "backend_schedule_name"

# The reserved request-field key the door binding rides as a plain (unprefixed) job kwarg between the
# create door and the ``schedule_task`` preparer. Like the prefixed namespace it is stamped by the
# platform from a validated field, never a tool kwarg, so the refusal rule covers it too.
SCHEDULE_STATE_BINDING_REQUEST_KEY = "state_binding"


class ReservedScheduleKeyError(ValueError):
    """A caller supplied a reserved schedule-door key the platform alone may stamp.

    Raised by :func:`assert_no_reserved_schedule_keys`; the skeleton's create door maps it to its
    ``BadRequestError`` (a 400) and a backend task preparer lets it propagate as the loud refusal a
    forged deferred fire deserves. The message names the offending key.
    """


def assert_no_reserved_schedule_keys(kwargs: dict[str, Any]) -> None:
    """Refuse any caller-supplied reserved schedule-door key in ``kwargs``, raising loudly if one is present.

    Every reserved door kwarg — any :data:`RESERVED_SCHEDULE_PREFIX`-prefixed key, plus the plain
    :data:`SCHEDULE_STATE_BINDING_REQUEST_KEY` — is STAMPED by the platform from a validated field. A
    caller one could forge the fire's subject, identity, binding or contract, so it is refused. The
    single exemption is :data:`SCHEDULE_NAME_KEY`, the caller's own cadence name; the exact
    ``backend_schedule`` cadence key does not match the door prefix and so is never a reserved key.

    The one refusal rule for every door that enqueues a backend job: the create door calls it before
    it stamps the validated door signals, and a background/task preparer calls it before it stamps the
    ambient fire — so a forged key can never survive to the worker's ``backend_fire`` pop.
    """
    for key in kwargs:
        if key.startswith(RESERVED_SCHEDULE_PREFIX) and key != SCHEDULE_NAME_KEY:
            raise ReservedScheduleKeyError(
                f"{key!r} is a reserved schedule door key stamped by the platform, not a tool kwarg"
            )
    if SCHEDULE_STATE_BINDING_REQUEST_KEY in kwargs:
        raise ReservedScheduleKeyError(
            f"{SCHEDULE_STATE_BINDING_REQUEST_KEY!r} is a reserved schedule key set from the request "
            "field, not a tool kwarg"
        )


# The ambient marker the create door lays around the ONE ``run_tool`` that dispatches a
# ``<tool>_schedule_task`` branch, AFTER it has refused caller-forged reserved keys and stamped the
# validated door signals. It proves a scheduled preparer's reserved ``backend_schedule_*`` kwargs came
# from the platform's own create fire — a caller who names the branch directly at the run-tool/MCP edge
# reaches the preparer with no fire on the stack. ``False`` outside a create fire; a contextvar so it
# flows through the in-process dispatch (and, if that dispatch is thread-offloaded, its copied context).
_schedule_create_fire: ContextVar[bool] = ContextVar("tai42_schedule_create_fire", default=False)


def in_schedule_create_fire() -> bool:
    """Whether the current dispatch is the platform's own schedule-create fire. A plain read, never raises."""
    return _schedule_create_fire.get()


@contextmanager
def schedule_create_fire() -> Iterator[None]:
    """Mark the wrapped dispatch as the platform's own schedule-create fire.

    The create door enters this around the ``run_tool`` that dispatches the ``<tool>_schedule_task``
    branch so the branch preparer can prove the reserved ``backend_schedule_*`` kwargs it carries came
    from the platform, not a caller who named the branch directly. Resets in a ``finally`` (token
    discipline).
    """
    token = _schedule_create_fire.set(True)
    try:
        yield
    finally:
        _schedule_create_fire.reset(token)


def assert_schedule_create_fire() -> None:
    """Refuse a ``schedule_task`` branch preparer entered outside the platform's schedule-create fire.

    The recurring branch preparer carries the reserved ``backend_schedule_*`` door kwargs the fire pops
    and honours as identity/binding/contract; those are stamped ONLY by the create door, which enters
    :func:`schedule_create_fire` around its dispatch. A caller who names ``<tool>_schedule_task``
    directly at the run-tool/MCP edge reaches the preparer with no create fire on the stack — refused
    loudly (the reserved-key error family) so a forged reserved key can never ride the job to the
    worker's ``backend_fire`` pop, nor a schedule register while skipping the create door's validation.
    """
    if not in_schedule_create_fire():
        raise ReservedScheduleKeyError(
            "a schedule_task branch may be registered only through the platform's schedule-create door; "
            "it was dispatched directly, so its reserved schedule keys cannot be trusted"
        )


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

# The reserved job kwargs a CONTRACT-bearing scheduled fire carries its firing identity and door
# contract under. The stored identity is the pair a park stores (the execution key's ``user_id`` and
# its per-mint fingerprint), derived from the schedule's ``execution_key`` at CREATE — the raw key
# never enters the queue in any other form. The contract is the four door jqs the fire evaluates.
SCHEDULE_EXECUTION_KEY_ARG = "backend_schedule_execution_key"
SCHEDULE_EXECUTION_FINGERPRINT_ARG = "backend_schedule_execution_fingerprint"
SCHEDULE_CONTRACT_ARG = "backend_schedule_contract"


def pop_schedule_subject(kwargs: dict[str, Any]) -> StateSubject | None:
    """Strip :data:`SCHEDULE_SUBJECT_ARG` from ``kwargs`` and parse it into a :class:`StateSubject`.

    Returns ``None`` when the job carries none. Mutates ``kwargs`` in place so the popped
    dispatch kwarg never reaches the tool, the same shape the worker's secret-capability pop
    uses. A malformed value raises loudly — a stamped-but-unparseable subject is a bug, never a
    silent skip.
    """
    raw = kwargs.pop(SCHEDULE_SUBJECT_ARG, None)
    if raw is None:
        return None
    if isinstance(raw, StateSubject):
        return raw
    return StateSubject.model_validate(raw)


def pop_schedule_state_binding(kwargs: dict[str, Any]) -> StateBinding | None:
    """Strip :data:`SCHEDULE_STATE_BINDING_ARG` from ``kwargs`` and parse it into a :class:`StateBinding`.

    Returns ``None`` when the job carries none. Mutates ``kwargs`` in place so the reserved
    kwarg never reaches the tool. A malformed value raises loudly — a stamped-but-unparseable
    binding is a bug, never a silent skip.
    """
    raw = kwargs.pop(SCHEDULE_STATE_BINDING_ARG, None)
    if raw is None:
        return None
    if isinstance(raw, StateBinding):
        return raw
    return StateBinding.model_validate(raw)


def pop_schedule_execution_identity(kwargs: dict[str, Any]) -> tuple[str, str] | None:
    """Strip the reserved execution-key/fingerprint pair from ``kwargs``, or ``None`` when absent.

    Returns ``(user_id, fingerprint)`` — the identity a contract-bearing scheduled fire binds to
    resume/rebind under. Both keys are stamped together or not at all; one present without the other
    is a stamped-but-corrupt job and raises loudly, never a silent half-identity.
    """
    user_id = kwargs.pop(SCHEDULE_EXECUTION_KEY_ARG, None)
    fingerprint = kwargs.pop(SCHEDULE_EXECUTION_FINGERPRINT_ARG, None)
    if user_id is None and fingerprint is None:
        return None
    if not isinstance(user_id, str) or not isinstance(fingerprint, str):
        raise TypeError(
            f"a scheduled fire's execution identity needs both {SCHEDULE_EXECUTION_KEY_ARG!r} and "
            f"{SCHEDULE_EXECUTION_FINGERPRINT_ARG!r} as strings, got {user_id!r} / {fingerprint!r}"
        )
    return user_id, fingerprint


def pop_schedule_contract(kwargs: dict[str, Any]) -> ParkableDoorMixin | None:
    """Strip :data:`SCHEDULE_CONTRACT_ARG` from ``kwargs`` and parse it into a :class:`ParkableDoorMixin`.

    Returns ``None`` when the job carries none. Mutates ``kwargs`` in place so the reserved kwarg never
    reaches the tool. A malformed value raises loudly — a stamped-but-unparseable contract is a bug.
    """
    raw = kwargs.pop(SCHEDULE_CONTRACT_ARG, None)
    if raw is None:
        return None
    if isinstance(raw, ParkableDoorMixin):
        return raw
    return ParkableDoorMixin.model_validate(raw)


@contextmanager
def schedule_subject_context(subject: StateSubject | None) -> Iterator[None]:
    """Enter the ``schedule``-door :func:`state_context` keyed on ``subject`` (context-free when ``None``).

    A schedule fire is anonymous/system, so ``actor`` is ``None``. With no subject the block runs
    context-free (its ``api`` door is stamped at the write chokepoint). This is the resolved-subject
    core of the schedule door, shared by the worker wrapper and the door-firing seam.
    """
    if subject is None:
        yield
        return
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


@contextmanager
def schedule_state_context(kwargs: dict[str, Any]) -> Iterator[None]:
    """Pop a worker job's reserved subject/binding kwargs and enter its ``schedule`` state context.

    The thin worker wrapper over :func:`schedule_subject_context`: it pops
    :data:`SCHEDULE_SUBJECT_ARG` and :data:`SCHEDULE_STATE_BINDING_ARG` from ``kwargs`` in place (so
    neither reaches the tool) and enters the ``schedule`` state context for the popped subject. The
    door-layer binding is applied by :func:`~tai42_kit.backend.schedule_fire.fire_schedule_door`
    around the started tool alone (never an ambient deposit), so it is stripped here but not
    deposited.
    """
    subject = pop_schedule_subject(kwargs)
    pop_schedule_state_binding(kwargs)
    with schedule_subject_context(subject):
        yield
