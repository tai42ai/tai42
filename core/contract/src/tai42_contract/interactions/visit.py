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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

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
    ``result``/``error`` instead of the question fields. Absent fields default to ``None`` and are left out
    of every dump: an entry serializes with only its set fields, never a null key, whichever door sends it.
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

    @model_serializer(mode="wrap")
    def _without_unset_fields(self, handler: SerializerFunctionWrapHandler):
        """The entry's dump with every ``None`` field left out (a set value keeps its own nested nulls).

        Unannotated on purpose: a return annotation would replace the entry's fields in its published
        serialization JSON schema.
        """
        return {key: value for key, value in handler(self).items() if value is not None}


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


class RunResultAnswer(BaseModel, frozen=True):
    """A run that finished: its value, whatever shape it has."""

    kind: Literal["result"] = "result"
    result: Any


class RunAsksAnswer(BaseModel, frozen=True):
    """A run that parked with asks addressed to its caller: their entries and the re-park sentinel."""

    kind: Literal["asks"] = "asks"
    asks: list[ParkedEntry]
    suspended: SuspendedInteraction


class RunParkedAnswer(BaseModel, frozen=True):
    """A run that parked on asks only someone else answers: the re-park sentinel alone."""

    kind: Literal["parked"] = "parked"
    suspended: SuspendedInteraction


class RunNoneAnswer(BaseModel, frozen=True):
    """A visit that ran nothing."""

    kind: Literal["none"] = "none"


# The answer a run door hands its caller, discriminated on ``kind`` so a reader never infers the
# outcome from the shape of a value: a finished run's own value rides inside ``result`` whatever it
# looks like.
RunAnswer = Annotated[RunResultAnswer | RunAsksAnswer | RunParkedAnswer | RunNoneAnswer, Field(discriminator="kind")]


@dataclass(frozen=True, slots=True)
class VisitAct:
    """A notice that the visit is about to act, or has just committed a claim.

    A door that passes ``on_act`` to :class:`Visit` is told where the visit crosses from checking to
    acting. The visit awaits the notice:

    - once before the cancels (``phase="before"``, ``interaction_ids`` the cancel ids);
    - twice per resume or take claim: right before the claim call (``phase="before"``) and right
      after that claim commits (``phase="committed"``), ``interaction_ids`` the one claimed id;
    - once before the start (``phase="before"``, ``interaction_ids`` empty).

    It is never awaited during the pre-check or the run-entry drain, and never when the visit does
    nothing. A raise from the notice propagates out of the visit. A raise from a ``before`` notice
    leaves nothing acted on by that action: no cancel, no claim, no start. After a ``committed``
    notice the claim stands, and a resume's continuation-due record is left for the interactions
    reaper.

    A door that recorded a ``before`` claim notice and never saw its ``committed`` notice reads it
    this way. If the visit then raised
    :class:`~tai42_contract.interactions.ParkedEntryGoneError`, the claim lost its race and did not
    commit. Otherwise (the claim call raised, or the process stopped) the claim may or may not have
    committed: :class:`SettleVisitClaim` then tells only whether each entry is still open. An entry
    still open shows the claim did not commit; an entry no longer open was resolved, by this claim or
    by any other resolution, and the read cannot tell which.
    """

    action: Literal["cancel", "resume", "take", "start"]
    interaction_ids: tuple[str, ...]
    phase: Literal["before", "committed"]


@runtime_checkable
class SettleVisitClaim(Protocol):
    """Tell which entries of a resume or take claim are no longer open in the interactions store."""

    async def __call__(self, act: VisitAct) -> tuple[str, ...]:
        """Return the ids of ``act.interaction_ids`` that are no longer open.

        A resume entry is open while its interaction state is ``pending``; a take entry is open while
        its outcome is still waiting. An empty result proves that no claim on these ids committed. A
        returned id was resolved by some door (this claim, an answer, an expiry, another caller, the
        retention sweep or a TTL), and which one is not known, so a caller must never read it as its
        own commit. A store fault raises.
        """
        ...


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
        on_act: Callable[[VisitAct], Awaitable[None]] | None = None,
    ) -> VisitOutcome:
        """Run the visit for one door and return its :class:`VisitOutcome`.

        ``target_name`` names the tool/agent the door drives. ``cancel`` is the ids whose whole run
        is killed; ``resume`` is the resume/take items acted on (at most one non-cancel action per
        visit). ``start`` starts the target with ``extras`` when nothing was resumed or taken
        (``None`` starts nothing); ``extras`` keys must be declared by the target. ``state_binding``
        is the door's binding, deposited ONLY around ``start`` — never around a resume/take/cancel
        continuation. ``receives_outcome`` is whether THIS caller takes the resumed run's outcome
        inline (a route, a holder node, ``resume_parked``, a direct run) or has no receiver (a hook,
        a schedule), which decides where the platform delivers a resumed run's terminal.
        ``on_act`` is awaited at each point where the visit acts, as :class:`VisitAct` states;
        ``None`` (the default) asks for no notice.
        """
        ...


__all__ = [
    "ParkableRunFailedError",
    "ParkedEntry",
    "ParkedEntryGoneError",
    "ParkedStatus",
    "RunAnswer",
    "RunAsksAnswer",
    "RunNoneAnswer",
    "RunParkedAnswer",
    "RunResultAnswer",
    "SettleVisitClaim",
    "UndeclaredExtrasKeyError",
    "Visit",
    "VisitAct",
    "VisitOutcome",
    "VisitRequestError",
]
