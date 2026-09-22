"""The shared visit: the ONE seam every door drives a parkable run through.

A door (a conversation route, a hook, a schedule, a direct run-tool call, an agent SSE stream)
resolves its own jqs to plain values and hands them to :class:`Visit`, which runs the whole
cancel / resume / take / start order once for every caller: it checks everything
before doing anything, cancels, resumes or takes at most one action besides cancel, starts the
target when nothing was resumed, normalises what came back into caller asks / a re-park / a final
result, and returns a :class:`VisitOutcome`.

The contract here is the CALL SHAPE and the two value types it exchanges (:class:`VisitOutcome`
and :class:`ParkedEntry`); the implementation lives in the skeleton. The resume/take items a door
passes are :class:`~tai42_contract.interactions.door_contract.ResumeItem` /
:class:`~tai42_contract.interactions.door_contract.TakeItem`; a still-suspended partial resume it
gets back is a :class:`~tai42_contract.interactions.models.ResumeBuffered`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from tai42_contract.errors import ErrorKind
from tai42_contract.interactions.models import SuspendedInteraction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from tai42_contract.interactions.door_contract import ResumeItem, TakeItem
    from tai42_contract.states.binding import StateBinding

# The lifecycle status a parked entry shows: a pending ask, an answered ask whose continuation is
# resuming, or a resolved run's waiting outcome (a clean finish or a stored failure).
ParkedStatus = Literal["asking", "running", "finished", "failed"]


class VisitRequestError(ValueError):
    """A structural refusal the visit pre-check makes BEFORE anything is cancelled or resumed.

    The one raise for every rule the pre-check enforces on the resolved cancel/resume request;
    ``rule`` names the violated rule so a caller can branch on it: ``cancel_resume_overlap`` (an id
    named in both ``cancel`` and ``resume``), ``multi_run`` (resume items spanning two steps or two
    held runs, or mixing a live resume with a settled take), ``finished_payload`` (a resume payload
    given on a ``finished``/``failed`` entry), ``user_ask_in_resume`` (a ``to="user"`` id named in
    ``resume``), or ``resume_extras`` (a resume carrying non-empty ``extras`` — extras are for a
    start only). Raised with NOTHING cancelled and NOTHING resumed.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT

    def __init__(self, rule: str, message: str) -> None:
        """Carry the violated ``rule`` name alongside the human ``message``."""
        super().__init__(message)
        self.rule = rule


class ParkedEntryGoneError(RuntimeError):
    """A cancel/resume/take id no longer names a live parked entry of the run's own list.

    Raised by the pre-check when an id is absent from the run's parked list (an id outside the list
    is gone), and by the resume claim when the atomic answer claim
    loses a race (the entry was answered/cancelled/expired between the pre-check and the claim). The
    entry named nothing to act on, so the visit surfaces it rather than silently skipping.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


class ParkableRunFailedError(RuntimeError):
    """A ``take`` of a ``failed`` waiting outcome — the resolved run had failed while it waited.

    Raised by a take of a ``failed`` entry, carrying the stored failure, so the node/door taking it
    fails exactly as if the run had failed while it waited rather than receiving a value.
    """

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, error: Any) -> None:
        """Carry the stored failure ``error`` the resolved run recorded."""
        super().__init__("the taken parkable run had failed while it waited")
        self.error = error


class UndeclaredExtrasKeyError(ValueError):
    """A start carried an ``extras`` key the target tool/agent did not declare reading.

    A tool declares the extras keys it reads at registration (``extras_keys=``); an agent target
    declares them through its ``extras_keys`` class attribute. The visit refuses an undeclared key
    BEFORE the run starts, so an extras injection never reaches a target that never asked for it.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class ParkedEntry(BaseModel):
    """One parked interaction on a run's subject — the full entry ``list_parked`` and ``$parked`` carry.

    ``id`` and ``status`` are always present. The question/answer fields are present for a state-backed
    entry (an ``asking`` or ``running`` one); a bare ``running`` membership whose state hash has aged
    out carries only ``id``/``status``, and a ``finished``/``failed`` waiting outcome carries its
    ``result``/``error`` instead of the question fields. Absent fields default to ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: ParkedStatus
    to: Literal["user", "caller"] | None = None
    asked_by: list[str] | None = None
    question: str | None = None
    answer_format: str | None = None
    format_payload: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    group_id: str | None = None
    created_at: str | None = None
    expiry_at: str | None = None
    on_expiry: Literal["kill", "resume"] | None = None
    thread_id: str | None = None
    channel: str | None = None
    recipient: str | None = None
    audience: str | None = None
    media: list[dict[str, Any]] | None = None
    result: Any = None
    error: Any = None


class VisitOutcome(BaseModel):
    """What one :class:`Visit` call did and hands its caller.

    ``action`` names the single non-cancel action taken; ``cancelled`` lists every id cancelled.
    ``kind`` classifies what came back and picks the payload field: ``result`` (a final value in
    ``result``), ``asks`` (caller asks parked — their full entries in ``asks`` AND the re-park
    sentinel over every still-open id of the step in ``suspended``, so a caller passing the step up
    hands back the sentinel itself), ``parked`` (only user asks left, the sentinel alone in
    ``suspended``), or ``none`` (nothing ran).
    """

    action: Literal["resumed", "taken", "started", "none"]
    cancelled: list[str] = []
    kind: Literal["result", "asks", "parked", "none"]
    result: Any = None
    asks: list[ParkedEntry] = []
    suspended: SuspendedInteraction | None = None


@runtime_checkable
class Visit(Protocol):
    """Drive a parkable run through the one shared cancel / resume / take / start order."""

    async def __call__(
        self,
        *,
        target_name: str,
        cancel: list[str],
        resume: list[ResumeItem | TakeItem],
        start: Callable[[Mapping[str, Any]], Awaitable[Any]] | None,
        extras: Mapping[str, Any],
        state_binding: StateBinding | None = None,
        receives_outcome: bool = True,
    ) -> VisitOutcome:
        """Run the visit for one door and return its :class:`VisitOutcome`.

        ``target_name`` names the tool/agent the door drives. ``cancel`` is the ids to whole-chain
        kill; ``resume`` is the resume/take items acted on (at most one non-cancel action per
        visit). ``start`` starts the target with ``extras`` when nothing was resumed or taken
        (``None`` starts nothing); ``extras`` keys must be declared by the target. ``state_binding``
        is the door's binding, deposited ONLY around ``start`` — never around a resume/take/cancel
        continuation. ``receives_outcome`` is whether THIS caller takes the resumed run's outcome
        inline (a route, a holder node, ``resume_parked``, a direct run) or has no receiver (a hook,
        a schedule), which decides where the platform delivers a resumed run's terminal.
        """
        ...


__all__ = [
    "ParkableRunFailedError",
    "ParkedEntry",
    "ParkedEntryGoneError",
    "ParkedStatus",
    "UndeclaredExtrasKeyError",
    "Visit",
    "VisitOutcome",
    "VisitRequestError",
]
