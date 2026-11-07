"""The typed fleet ops the worker bus carries: each op's name, its fields, and what applying it changes.

The dispatcher owns this vocabulary. A publisher builds one of these models and the bus publishes
its dump; a receiver validates the delivered dict back into the model (an unknown op or a missing
field is refused before anything applies) and reports the op's declared ``effects`` to the
post-apply handlers, and whether a resyncing worker owes it a reply. A dump that excludes
defaults is the exact wire dict: the tag is always present, a defaulted field only when set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from tai42_contract.backend import FleetOpEffect

from tai42_skeleton.app.bus.exit_latch import ExitRefusal, ExitRefusalKind
from tai42_skeleton.app.bus.models import FleetResult, WorkerIdentity, WorkerKind

__all__ = [
    "FLEET_OP_ADAPTER",
    "CarryRoll",
    "CarryRollReply",
    "ClearTemplateCache",
    "DeploymentKeysChanged",
    "DeregisterMcp",
    "EvictTemplate",
    "FleetOp",
    "ListFailedMcps",
    "Recycle",
    "ReleaseRoll",
    "ReleaseRollReply",
    "ReloadConfig",
    "ReloadFailedMcps",
    "ReloadMcp",
    "ReloadTool",
    "RemoveTool",
    "ReprobeFailedMcps",
    "carry_roll_reply",
    "fleet_op_wire",
    "recycle_leaving",
    "recycle_reply",
    "release_roll_reply",
]

_REGISTRY: frozenset[FleetOpEffect] = frozenset({FleetOpEffect.registry})
_TEMPLATES: frozenset[FleetOpEffect] = frozenset({FleetOpEffect.templates})
_NONE: frozenset[FleetOpEffect] = frozenset()


class _FleetOp(BaseModel):
    """A fleet op's fields, its declared ``effects``, and whether a resyncing worker owes it a reply.

    A fresh ``resyncing`` worker is subscribed and applies an op it is sent after its resync. An
    op whose reply the publisher needs from such a worker declares ``awaited_mid_resync``; every
    other op leaves a resyncing worker a gap that its own resync re-reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    effects: ClassVar[frozenset[FleetOpEffect]]
    awaited_mid_resync: ClassVar[bool] = False


class ReloadConfig(_FleetOp):
    """Re-read the persisted config and rebuild the registries."""

    op: Literal["reload_config"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReloadMcp(_FleetOp):
    """Reload one mounted MCP server by title."""

    op: Literal["reload_mcp"]
    title: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class DeregisterMcp(_FleetOp):
    """Drop one mounted MCP server by title."""

    op: Literal["deregister_mcp"]
    title: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReloadTool(_FleetOp):
    """Rebind one tool of a kind by name."""

    op: Literal["reload_tool"]
    kind: str
    name: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class RemoveTool(_FleetOp):
    """Unbind one tool of a kind by name."""

    op: Literal["remove_tool"]
    kind: str
    name: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReloadFailedMcps(_FleetOp):
    """Retry every MCP server that failed to mount."""

    op: Literal["reload_failed_mcps"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ReprobeFailedMcps(_FleetOp):
    """Re-probe the named MCP servers that failed to mount, mounting each that answers now."""

    op: Literal["reprobe_failed_mcps"]
    titles: list[str]
    effects: ClassVar[frozenset[FleetOpEffect]] = _REGISTRY


class ListFailedMcps(_FleetOp):
    """Report the MCP servers that failed to mount; changes nothing."""

    op: Literal["list_failed_mcps"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE


class EvictTemplate(_FleetOp):
    """Drop one compiled template, or every template under ``path`` when ``prefix`` is set."""

    op: Literal["evict_template"]
    path: str
    prefix: bool = False
    effects: ClassVar[frozenset[FleetOpEffect]] = _TEMPLATES


class ClearTemplateCache(_FleetOp):
    """Drop the whole compiled-template cache."""

    op: Literal["clear_template_cache"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _TEMPLATES


class Recycle(_FleetOp):
    """Gracefully exit this worker after its reply ships; changes nothing it keeps.

    The reply is ``{"recycling": kind}`` when the worker armed its exit. A worker that arms
    nothing names why (:func:`recycle_reply`): one already leaving (``already_leaving``: its
    reason) leaves once, and one whose exit is deferred to its own in-flight change
    (``exit_deferred``: that change) leaves when that change's reply is answered.
    """

    op: Literal["recycle"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE


def recycle_reply(worker_kind: str, refusal: ExitRefusal | None) -> dict[str, str]:
    """The ``applied`` payload a worker of ``worker_kind`` answers a :class:`Recycle` op with.

    ``refusal`` is why it armed no exit (:class:`~tai42_skeleton.app.bus.ExitRefusal`): the
    payload names it under its kind with its reason. ``None``: the exit was armed.
    """
    if refusal is None:
        return {"recycling": worker_kind}
    return {"recycling": worker_kind, refusal.kind: refusal.reason}


def recycle_leaving(payload: object) -> ExitRefusal | None:
    """The refusal an ``applied`` :class:`Recycle` reply names, else ``None`` (the exit was armed).

    Raises :class:`TypeError` when the reply names a refusal whose reason is not text.
    """
    named = _named_reason(payload, get_args(ExitRefusalKind))
    return None if named is None else ExitRefusal(kind=named[0], reason=named[1])


def _named_reason[K: str](payload: object, kinds: tuple[K, ...]) -> tuple[K, str] | None:
    """The first of ``kinds`` an op's ``applied`` ``payload`` names, with the reason it carries; ``None`` when none.

    A payload that is not an object names none. Raises :class:`TypeError` when the named
    reason is not text: the payload breaks the reply the op defines.
    """
    if not isinstance(payload, dict):
        return None
    for kind in kinds:
        reason = payload.get(kind)
        if reason is None:
            continue
        if not isinstance(reason, str):
            raise TypeError(
                f"fleet op reply: {kind!r} names its reason as {type(reason).__name__}, not text: {reason!r}"
            )
        return kind, reason
    return None


class DeploymentKeysChanged(_FleetOp):
    """A worker joined the bus: withdraw the applied stored keys the fleet's deployment now owns.

    Carries no names: each receiver re-reads the census, so it judges by every row as it stands.
    A receiver that withdrew a key rebuilds its serving generation through the config reload it
    applies (that reload carries the registry effect), and replies with the withdrawn names and
    every recycle-class key it still runs on off its effective value, as every reload replies.
    The op itself changes nothing, so it declares no effects; its reload does. A worker still
    resyncing at publish owes the reply too, since only the joiner can roll it: its own resync
    does not.
    """

    op: Literal["deployment_keys_changed"]
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE
    awaited_mid_resync: ClassVar[bool] = True


class CarryRoll(_FleetOp):
    """Carry out the fleet recycle roll a change decided, as this worker's own tracked task.

    Sent by a change run under a bound the roll's waits can outlast (a dispatched job's budget, an
    armed turn budget) to the bus member it acts for, the change's writer. It carries
    the decision's inputs: the change's reload ``report``, the recycle targets ``pinned`` at the
    change's start, the ``writer`` and the workers a targeted reload ``addressed`` (``None``: the
    whole fleet) — names, kinds, generations and key names, never an env value. ``writer_exits``
    says whether the writer's own restart may follow the roll. ``holds`` names the roll holds
    (:mod:`~tai42_skeleton.config.roll_holds`) the member keeps from before its answer until the
    roll ended. ``roll`` is the roll's identity, which the run's release names; ``answers_within``
    the seconds left of the run's bound at the hand-off: the latest the run answers. The member
    answers (:class:`CarryRollReply`) as soon as it took those holds, then holds the roll until
    the run released it (:class:`ReleaseRoll`), at its bound's exit, once it answered; over the
    bus, where the run is in another process and can end with no word, it starts the roll once
    ``answers_within`` passed with no release.
    """

    op: Literal["carry_roll"]
    change: str
    roll: str
    answers_within: float
    report: FleetResult
    pinned: dict[str, tuple[WorkerKind, int]]
    writer: WorkerIdentity
    addressed: tuple[str, ...] | None = None
    writer_exits: bool = True
    holds: tuple[str, ...] = ()
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE
    awaited_mid_resync: ClassVar[bool] = True


CarryRollReplyKind = Literal["carrying", "already_leaving"]


@dataclass(frozen=True)
class CarryRollReply:
    """How a member answered a :class:`CarryRoll` op, and the reason it names.

    ``carrying``: it took the roll, ``reason`` the change it carries. ``already_leaving``: its
    exit latch is set, ``reason`` the exit's, and it carries nothing.
    """

    kind: CarryRollReplyKind
    reason: str

    def reply(self) -> dict[str, str]:
        """The ``applied`` payload the member answers with."""
        return {self.kind: self.reason}


def carry_roll_reply(payload: object) -> CarryRollReply | None:
    """The :class:`CarryRollReply` an ``applied`` :class:`CarryRoll` payload names, else ``None`` (not such a reply).

    Raises :class:`TypeError` when the reply names a kind whose reason is not text.
    """
    named = _named_reason(payload, get_args(CarryRollReplyKind))
    return None if named is None else CarryRollReply(kind=named[0], reason=named[1])


class ReleaseRoll(_FleetOp):
    """Start the roll a :class:`CarryRoll` op handed this worker: the run that handed it off has answered.

    Sent at the exit of the run's bound (:func:`~tai42_kit.utils.run_bound.bounded_run`), after
    its body returned or raised, to the member holding the roll ``roll`` for ``change``. The
    member answers (:class:`ReleaseRollReply`) whether it started the roll now or had started it
    already; it fails the op when it holds no such roll. Changes nothing a worker keeps.
    """

    op: Literal["release_roll"]
    roll: str
    change: str
    effects: ClassVar[frozenset[FleetOpEffect]] = _NONE
    awaited_mid_resync: ClassVar[bool] = True


ReleaseRollReplyKind = Literal["started", "already_started"]


@dataclass(frozen=True)
class ReleaseRollReply:
    """How a member answered a :class:`ReleaseRoll` op, and the change it names.

    ``started``: it started the held roll on the release. ``already_started``: the roll had
    started before this release came, because the run's bound passed or an earlier release
    started it.
    """

    kind: ReleaseRollReplyKind
    reason: str

    def reply(self) -> dict[str, str]:
        """The ``applied`` payload the member answers with."""
        return {self.kind: self.reason}


def release_roll_reply(payload: object) -> ReleaseRollReply | None:
    """The :class:`ReleaseRollReply` an ``applied`` :class:`ReleaseRoll` payload names, else ``None`` (no such reply).

    Raises :class:`TypeError` when the reply names a kind whose reason is not text.
    """
    named = _named_reason(payload, get_args(ReleaseRollReplyKind))
    return None if named is None else ReleaseRollReply(kind=named[0], reason=named[1])


FleetOp = Annotated[
    ReloadConfig
    | ReloadMcp
    | DeregisterMcp
    | ReloadTool
    | RemoveTool
    | ReloadFailedMcps
    | ReprobeFailedMcps
    | ListFailedMcps
    | EvictTemplate
    | ClearTemplateCache
    | Recycle
    | CarryRoll
    | ReleaseRoll
    | DeploymentKeysChanged,
    Field(discriminator="op"),
]

FLEET_OP_ADAPTER: TypeAdapter[FleetOp] = TypeAdapter(FleetOp)


def fleet_op_wire(op: FleetOp) -> dict[str, Any]:
    """The wire dict a fleet op travels as: its fields in JSON form, defaults omitted."""
    return op.model_dump(mode="json", exclude_defaults=True)
