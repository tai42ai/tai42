"""The driver-continuation context for an async ``ask``.

Two generic context variables naming registered tools a resuming driver binds:

* the RESUME continuation — the tool that resumes the CURRENT driver when a tool
  async-suspends. The resuming driver SETS it around a tool dispatch; the platform
  READS it when an ``ask`` is raised with ``mode="async"``, stamping the value
  onto the parked interaction as its ``continuation_tool``.
* the COMPLETION continuation — the :class:`DeliveryAddress` a driver fires with the FINAL
  answer when a resumed run drives to a clean terminal (not a re-park), so a deferred response
  is delivered out of band: the delivery tool, an opaque routing context the tool reads to route
  that answer back, and the conversation thread the run belongs to. A driver binds it around a
  run and keeps it with its own resume state; an address naming no tool (the default) means no
  completion delivery is wired.

The resume continuation is a bare tool name. Both are a generic platform mechanism that never
names a flow, a session, or any engine state — the address's ``context`` is fully opaque to this
contract (it carries no route/flow/thread meaning here) and MUST be JSON-serializable, since a
durable resume path round-trips it. A resume name of ``None`` (the default) means no resuming
driver is bound, and an async ask raised there fails loudly.

The rest is shared vocabulary: the chain routing a platform seam binds around a nested dispatch
and the keys its fires carry, the completion statuses, the park marker's reserved key, and the
signals and errors that cross the delivery seam. The logic over them — park adoption, the marker
builder/reader, the kill handlers, the outcome text — lives in ``tai42_kit.interactions``.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, model_validator

from tai42_contract.errors import ErrorKind

__all__ = [
    "CHAINED_PARK_KEY_PREFIX",
    "CHAINED_PARK_TOKEN_KEY",
    "EXPIRY_ANSWER",
    "PARK_COMPLETION_FAILED",
    "PARK_COMPLETION_REPARKED",
    "PARK_COMPLETION_SUCCEEDED",
    "ChainedResume",
    "DeliveryAddress",
    "NestedParkOwnershipError",
    "ParkDeliveryUnauthorizedError",
    "ParkResumeUnauthorizedError",
    "RunTerminalFailed",
    "chained_lineage",
    "get_chained_resume",
    "get_park_completion",
    "get_resume_continuation_tool",
    "reset_chained_resume",
    "reset_park_completion",
    "reset_resume_continuation_tool",
    "set_chained_resume",
    "set_park_completion",
    "set_resume_continuation_tool",
]

_resume_continuation_tool: ContextVar[str | None] = ContextVar("tai42_resume_continuation_tool", default=None)

# The keys the completion fire itself writes into its payload beside the bound context; a
# context carrying one of them would be silently overwritten by the fire, so it is refused.
_FIRE_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset({"thread_id", "result", "completion_id", "status"})


class DeliveryAddress(BaseModel, frozen=True, extra="forbid"):
    """Where a run's deferred final answer is delivered.

    The delivery tool, its opaque routing context, and the conversation thread the run belongs
    to. Every field is required, so a stored address that lacks one fails to decode loudly.
    """

    tool: str | None
    context: dict[str, Any] | None
    thread_id: str | None

    @model_validator(mode="after")
    def _reserved_keys(self) -> DeliveryAddress:
        clash = sorted(set(self.context or {}) & _FIRE_PAYLOAD_KEYS)
        if clash:
            raise ValueError(f"completion context may not carry the fire payload's own key(s) {clash}")
        return self


_NO_DELIVERY: Final[DeliveryAddress] = DeliveryAddress(tool=None, context=None, thread_id=None)

_park_completion: ContextVar[DeliveryAddress] = ContextVar("tai42_park_completion", default=_NO_DELIVERY)


class ChainedResume(NamedTuple):
    """The cross-driver chain routing a nested driver captures at park time.

    A run whose nested tool ran on ANOTHER driver re-enters that waiting run ACROSS drivers by
    firing a chain-delivery tool at its own terminal. The three facts that fire needs ride this
    contextvar, bound by the platform around the nested dispatch and captured by the nested driver
    onto its own park index (never on ``_park_completion``, which is left carrying only the door's
    out-of-band address so it flows down unchanged):

    * ``delivery_tool`` — the chain-delivery tool the terminal fires to re-enter the ancestor;
    * ``chain_key`` — the key the ancestor's park is recorded under and the fire reverses back to it;
    * ``asked_by`` — the ANCESTOR's own call chain, passed on the chain re-entry as
      ``continues_chain`` so the ancestor's re-park records its OWN chain, not the descendant's plus
      the chain tool's name.
    """

    delivery_tool: str
    chain_key: str
    asked_by: tuple[str, ...]


class _ChainedFrame(NamedTuple):
    """The chain routing bound around the current nested dispatch and the chain keys it is nested in.

    ``lineage`` holds the chain key of every chained dispatch the current context is nested in,
    outermost first, the current routing's own key last; it is empty when ``routing`` is ``None``.
    """

    routing: ChainedResume | None
    lineage: tuple[str, ...]


# The frame outside any chained dispatch: no routing, an empty lineage.
_UNCHAINED: Final[_ChainedFrame] = _ChainedFrame(None, ())

_chained_resume: ContextVar[_ChainedFrame] = ContextVar("tai42_chained_resume", default=_UNCHAINED)


def get_chained_resume() -> ChainedResume | None:
    """The chain routing bound around the current nested dispatch, or ``None`` when unchained."""
    return _chained_resume.get().routing


def chained_lineage() -> tuple[str, ...]:
    """The chain keys of every chained dispatch the current context is nested in, outermost first.

    The innermost entry is the key of :func:`get_chained_resume`; empty when unchained. A run that
    parks here is nested under each of these chained calls, so each is a call whose waiting caller
    the run's terminal may re-enter.
    """
    return _chained_resume.get().lineage


def set_chained_resume(routing: ChainedResume | None) -> Token[_ChainedFrame]:
    """Bind ``routing`` as the current nested dispatch's chain routing and return the reset token.

    A routing extends the enclosing lineage with its chain key, unless that key is already the
    innermost one (a driver re-binding, for its resume, the routing it captured at park time). A
    ``None`` routing starts a new, empty lineage: a dispatch nothing can wait on keeps no chain of
    an outer dispatch.
    """
    if routing is None:
        return _chained_resume.set(_UNCHAINED)
    lineage = _chained_resume.get().lineage
    if not lineage or lineage[-1] != routing.chain_key:
        lineage = (*lineage, routing.chain_key)
    return _chained_resume.set(_ChainedFrame(routing, lineage))


def reset_chained_resume(token: Token[_ChainedFrame]) -> None:
    """Restore the chain routing and its lineage to the values captured in ``token``."""
    _chained_resume.reset(token)


# --- the platform delivery signals ------------------------------------------------------
#
# A resuming driver's continuation face RETURNS the outermost run's next outcome; the platform's
# delivery chokepoint reads that return and delivers it. Two exception signals cross that seam.


class RunTerminalFailed(Exception):  # noqa: N818 (a control-flow signal, not an *Error condition)
    """A run reached a TERMINAL that must be delivered FAILED — do NOT retry.

    A driver's run face RAISES this on every failed terminal — a first run and a resume alike —
    carrying the failed ``outcome`` as its payload. Each caller handles the one raise by what it
    owns: the platform (a conversation turn, a door, the delivery chokepoint) records and delivers
    the ``outcome`` WHOLE with a FAILED status, never reading a key inside it; a driver that nests
    another run catches it and reads the ``outcome`` its own engine wrote. Distinct from a PLAIN
    raise, which means "transient — retain the due record and redeliver".

    A RETURN from a run face is never a failure: it is a SUCCESS value, or a still-parked typed
    contract value (:class:`~tai42_contract.interactions.SuspendedInteraction` /
    :class:`~tai42_contract.interactions.ResumeBuffered`). A failure is always this raise.

    ``outcome`` is an OPAQUE payload the platform never inspects: it is written by whichever driver
    raised the failure and read only by that driver (or a run of the same engine). It MUST be a
    plain, JSON-serializable mapping, since a driver whose nested-dispatch seam serializes the
    failure (a checkpointed graph) carries ``outcome`` across that seam and re-raises this type with
    the payload intact.
    """

    def __init__(self, outcome: Any) -> None:
        """Carry the failed terminal ``outcome`` the ladder delivers with a FAILED status."""
        super().__init__("run reached a terminal to deliver FAILED")
        self.outcome = outcome


class ParkResumeUnauthorizedError(Exception):
    """A driver continuation face was invoked outside its run's own resume drive.

    A driver's resume face and cross-driver chain-delivery tool are dispatchable by name at the
    run-tool door and the MCP edge, so the platform gates them: a face may produce a run's outcome
    ONLY inside the platform's resume of that run. Raised when the ambient run-authorization
    context does not name the resumed interaction, its run's delivery identity, or a chained call
    the resumed interaction is nested under.
    """

    # A refusal to authorize the caller, not a caller input error or a transient unavailability.
    __tai_error_kind__ = ErrorKind.UNAUTHORIZED


class ParkDeliveryUnauthorizedError(Exception):
    """A door's delivery-address tool was invoked outside the platform's own delivery fire.

    A door's out-of-band delivery address is a registered tool, dispatchable by name at the
    run-tool door and the MCP edge; only the platform's delivery ladder may fire it. Raised when
    the ambient delivery-fire context is absent or names a different ``completion_id`` than the one
    the tool was asked to deliver.
    """

    # A refusal to authorize the caller, not a caller input error or a transient unavailability.
    __tai_error_kind__ = ErrorKind.UNAUTHORIZED


# The answer value a continuation receives when its interaction expired unanswered — the
# generic marker a resuming consumer reads to run its expiry branch instead of a real answer.
EXPIRY_ANSWER: Final[dict[str, bool]] = {"tai42:interaction_expired": True}


# The shared terminal-status vocabulary a completion fire names its outcome with. The
# resuming driver (which owns the parked run's terminal) and the delivery tool (which maps
# the outcome back to the caller) both key on these SAME two strings, so neither side has to
# name the other to agree on the wire vocabulary — the contract is the single shared point.
#
# The generic completion-fire payload the platform dispatches the address's delivery tool with
# is the opaque bound context merged with the thread and the terminal outcome:
#
#     {**address.context, "thread_id": <str|None>, "result": <terminal outcome>,
#      "completion_id": <str>, "status": <status>}
#
# where ``address`` is the bound :class:`DeliveryAddress`, ``result`` is the run's terminal
# outcome value, ``completion_id`` is the stable idempotency id of the resolved terminal, and
# ``status`` is one of the two constants below.
# ``PARK_COMPLETION_SUCCEEDED`` names a clean-success terminal whose ``result`` the delivery
# tool maps back to the caller; ``PARK_COMPLETION_FAILED`` names ANY non-success terminal
# (failed/stopped/aborted/errored) the delivery tool surfaces as its uniform notice. A
# delivery tool treats ``FAILED`` as the fail-safe default, so an unstamped fire never pushes
# a non-success payload through the success mapping.
PARK_COMPLETION_SUCCEEDED: Final[str] = "succeeded"
PARK_COMPLETION_FAILED: Final[str] = "failed"

# The one NON-terminal status in the vocabulary: the parked run did not finish, it raised a
# NEW ask under this same completion binding. The fire carries the new ``expiry_at`` in place
# of a ``result``, so a binder whose OWN suspension horizon was inherited from the run's
# previous ask can refresh it (``tai42_kit.interactions.park_adoption.repark_notice``). Only a
# CHAINED binding is notified — a delivery tool that has no horizon of its own is never fired with it — and a re-park
# notice never resolves anything: the completion still fires later under a terminal status.
PARK_COMPLETION_REPARKED: Final[str] = "reparked"


class NestedParkOwnershipError(RuntimeError):
    """Raised when a caller would ADOPT a park raised under a DIFFERENT resume binding.

    A park has exactly one resume owner: the driver whose continuation the platform
    stamped onto the parked interaction. Only that owner is ever resumed for it, so a
    caller that turns a returned park sentinel into its own suspended state — recording
    resume state of its own against the same interaction — would wait for a resume that
    is never fired at it, and its own run would hang forever.
    """

    # The park is already claimed by another driver: a state conflict, not a caller input
    # error and not a transient unavailability.
    __tai_error_kind__ = ErrorKind.CONFLICT


# --- chained parks ---------------------------------------------------------------------
#
# A CHAINED call is a nested dispatch a chain routing is bound around: the caller parks on the
# CALL rather than on whatever interaction the nested run parks on, and the nested run's terminal
# fires the routing's delivery tool, which re-enters the caller with that terminal. The shared
# vocabulary lives here because several parties touch it: the platform seam that binds the
# routing, the kit's adoption seam that converts a returned park sentinel onto the chain key, and
# the delivery tool that reads the fire back.

# Namespace prefix on every chained resume key, so a key naming a CALL is never mistaken for a
# platform interaction id (they share one key space wherever a driver indexes its parks).
CHAINED_PARK_KEY_PREFIX: Final[str] = "tai42:chained-park:"

# The chained resume key, carried under this key in every chain fire's payload: the key the
# CALLER's park is recorded under and the delivery tool reverses back to it.
CHAINED_PARK_TOKEN_KEY: Final[str] = "chain_token"  # noqa: S105 constant identifier, not a secret value


def get_resume_continuation_tool() -> str | None:
    """The tool that resumes the current driver if a tool async-suspends, or ``None`` if none bound.

    ``None`` is the default, and applies to any code outside a driver dispatch.
    """
    return _resume_continuation_tool.get()


def set_resume_continuation_tool(tool_name: str | None) -> Token[str | None]:
    """Bind ``tool_name`` as the current driver's resume continuation and return the reset token.

    The driver calls this around a tool dispatch; pass the returned
    token to :func:`reset_resume_continuation_tool` to restore the previous value.
    """
    return _resume_continuation_tool.set(tool_name)


def reset_resume_continuation_tool(token: Token[str | None]) -> None:
    """Restore the resume continuation to the value captured in ``token``.

    ``token`` comes from the matching :func:`set_resume_continuation_tool` call.
    """
    _resume_continuation_tool.reset(token)


def get_park_completion() -> DeliveryAddress:
    """The :class:`DeliveryAddress` a driver fires with a resumed run's FINAL answer.

    An address naming no tool, no context and no thread when no completion delivery is bound
    (the default, and any code outside a bound run).
    """
    return _park_completion.get()


def set_park_completion(
    tool: str | None = None, context: Mapping[str, Any] | None = None, *, thread_id: str | None = None
) -> Token[DeliveryAddress]:
    """Bind the current run's completion address and return the reset token.

    ``context`` is an opaque value the delivery tool reads to route the answer, and ``thread_id``
    the conversation thread the run belongs to. A driver calls this around a run whose deferred
    final answer must be delivered out of band; ``context`` is treated as fully opaque here and
    MUST be JSON-serializable. Pass the returned token to :func:`reset_park_completion` to restore
    the previous value.

    ``tool`` defaults to ``None``: a driver on a run-face that carries no out-of-band
    delivery still binds a completion (typically to reset a prior binding for the nested
    run), naming no delivery tool.

    ADDRESS-TOOL CONTRACT: a ``tool`` bound here is an out-of-band delivery ADDRESS that
    only the platform's delivery ladder may fire. Every such address tool asserts the
    platform's delivery-fire context at entry — its first statement is
    ``tai42_app.interactions.assert_delivery_authorized(completion_id)`` on the payload's
    ``completion_id`` — so a caller naming it directly at the run-tool door or the MCP edge
    is refused (:class:`ParkDeliveryUnauthorizedError`) before it delivers anything.
    """
    address = DeliveryAddress(tool=tool, context=dict(context) if context is not None else None, thread_id=thread_id)
    return _park_completion.set(address)


def reset_park_completion(token: Token[DeliveryAddress]) -> None:
    """Restore the completion continuation to the value captured in ``token``.

    ``token`` comes from the matching :func:`set_park_completion` call.
    """
    _park_completion.reset(token)
