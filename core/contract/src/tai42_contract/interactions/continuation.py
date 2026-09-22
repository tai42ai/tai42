"""The driver-continuation context for an async ``ask``.

Two generic context variables naming registered tools a resuming driver binds:

* the RESUME continuation — the tool that resumes the CURRENT driver when a tool
  async-suspends. The resuming driver SETS it around a tool dispatch; the platform
  READS it when an ``ask`` is raised with ``mode="async"``, stamping the value
  onto the parked interaction as its ``continuation_tool``.
* the COMPLETION continuation — the tool a driver fires with the FINAL answer when a
  resumed run drives to a clean terminal (not a re-park), so a deferred response is
  delivered out of band, PLUS an opaque address context the delivery tool reads to route
  that answer back. A driver binds both around a run and keeps them with its own resume
  state — the platform stores no completion field on the interaction; a name of ``None``
  (the default) means no completion delivery is wired.

A park belongs to exactly ONE resume continuation — the one bound when it was raised — and
carries that owner BOTH on its sentinel and on its wire-form marker. :func:`assert_park_adoptable`
is the guard every seam that turns a park into the CALLER's own suspended state applies: the
object seams that adopt a returned sentinel, and the CLAIM point where a driver records resume
state against a marker read off a serialized tool result. So a nested run's park is never adopted
by its caller (which would then wait for a resume fired only at the nested run).

The owner is the continuation NAME, not a per-run token, because that is the whole of what the
platform dispatches: the name is what the interaction stores and what a later answer invokes, so
a scoped owner would have to be unscoped again before dispatch and would fork the meaning of the
resume binding. Name equality alone is not what makes the guard sound — two runs of the same
driver share a name — the ``None`` rule is: a park crossing a nested RUN's face carries no owner
at all, so a same-named sibling is refused on that, never on a name comparison happening to
differ.

Refusing is not the only answer to a park this caller does not own. :func:`resolve_park_adoption`
is the OBJECT seams' form of the guard, and it answers adopt-your-own or CHAIN: a nested dispatch
a CHAIN routing was bound around parks on the nested CALL — a key of its own, owned by its own
resume continuation — while the nested run keeps its park and its resume, and its terminal
re-enters the caller through the routing's delivery tool. A dispatch nothing chained still gets
the loud refusal. The CLAIM point cannot chain: a park reaching it as content
belongs to no dispatch it could attribute the call to, so there it is adopt-or-refuse.

The resume continuation is a bare tool name; the completion continuation is a tool name
paired with an opaque context. Both are a generic platform mechanism that never names a
flow, a session, or any engine state — the context is fully opaque to this contract (it
carries no route/flow/thread meaning here) and MUST be JSON-serializable, since a durable
resume path round-trips it. A resume name of ``None`` (the default) means no resuming
driver is bound, and an async ask raised there fails loudly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar, Token
from datetime import datetime
from typing import Any, Final, NamedTuple, cast

from tai42_contract.errors import ErrorKind

logger = logging.getLogger(__name__)

__all__ = [
    "CHAINED_PARK_KEY_PREFIX",
    "CHAINED_PARK_TOKEN_KEY",
    "EXPIRY_ANSWER",
    "PARK_COMPLETION_FAILED",
    "PARK_COMPLETION_REPARKED",
    "PARK_COMPLETION_SUCCEEDED",
    "PARK_COMPLETION_THREAD_KEY",
    "SUSPENDED_INTERACTION_MARKER_KEY",
    "ChainedResume",
    "NestedParkOwnershipError",
    "ParkDeliveryUnauthorizedError",
    "ParkResumeFailed",
    "ParkResumeUnauthorizedError",
    "assert_park_adoptable",
    "attach_chained_park",
    "bound_execution_identity_for_fire",
    "chained_park_claims",
    "current_execution_identity",
    "fire_park_killed",
    "get_chained_resume",
    "get_park_completion",
    "get_resume_continuation_tool",
    "is_chained_park_key",
    "new_chained_park_key",
    "read_suspended_interaction_marker",
    "register_execution_identity_accessor",
    "register_execution_identity_binder",
    "register_park_kill_handler",
    "repark_notice",
    "reset_chained_resume",
    "reset_park_completion",
    "reset_resume_continuation_tool",
    "resolve_park_adoption",
    "set_chained_resume",
    "set_park_completion",
    "set_resume_continuation_tool",
    "suspended_interaction_marker",
]

_resume_continuation_tool: ContextVar[str | None] = ContextVar("tai42_resume_continuation_tool", default=None)

# The completion binding: a (tool name, opaque context) pair a driver fires on a clean
# terminal drive. Held as ONE immutable tuple so both halves bind and reset atomically. The
# default ``(None, None)`` means no completion delivery is wired.
_ParkCompletion = tuple[str | None, Mapping[str, Any] | None]

_park_completion: ContextVar[_ParkCompletion] = ContextVar("tai42_park_completion", default=(None, None))


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


_chained_resume: ContextVar[ChainedResume | None] = ContextVar("tai42_chained_resume", default=None)


def get_chained_resume() -> ChainedResume | None:
    """The chain routing bound around the current nested dispatch, or ``None`` when unchained."""
    return _chained_resume.get()


def set_chained_resume(routing: ChainedResume | None) -> Token[ChainedResume | None]:
    """Bind ``routing`` as the current nested dispatch's chain routing and return the reset token."""
    return _chained_resume.set(routing)


def reset_chained_resume(token: Token[ChainedResume | None]) -> None:
    """Restore the chain routing to the value captured in ``token``."""
    _chained_resume.reset(token)


# --- the platform delivery signals ------------------------------------------------------
#
# A resuming driver's continuation face RETURNS the outermost run's next outcome; the platform's
# delivery chokepoint reads that return and delivers it. Two exception signals cross that seam.


class ParkResumeFailed(Exception):  # noqa: N818 (a control-flow signal, not an *Error condition)
    """A resumed run reached a TERMINAL that must be delivered FAILED — do NOT retry.

    A resuming driver RAISES this from its continuation face for a mid-drive ABORT or SUPERSEDE
    terminal, carrying the failed ``outcome``. The delivery chokepoint catches it, CLEARS the
    continuation-due record (no redelivery), and delivers ``outcome`` through the ladder with a
    FAILED status. Distinct from a PLAIN raise, which means "transient — retain the due record and
    redeliver", and from an ordinary business-error terminal, which the face RETURNS as a
    FAILED-status outcome rather than raising.
    """

    def __init__(self, outcome: Any) -> None:
        """Carry the failed terminal ``outcome`` the ladder delivers with a FAILED status."""
        super().__init__("park resume reached a terminal to deliver FAILED")
        self.outcome = outcome


class ParkResumeUnauthorizedError(Exception):
    """A driver continuation face was invoked outside its run's own resume drive.

    A driver's resume face and cross-driver chain-delivery tool are dispatchable by name at the
    run-tool door and the MCP edge, so the platform gates them: a face may produce a run's outcome
    ONLY inside the platform's resume of that run. Raised when the ambient run-authorization
    context does not name the resumed interaction (or its run's delivery identity).
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


# --- the execution-identity bridge -------------------------------------------------------
#
# A park records the execution identity its run is authorized as, so an OUT-OF-BAND completion
# fired for it later runs under that same identity — never fail-open. The
# identity machinery is a HOST concern (it reads live stored grants), so this contract holds only
# a pair of registration slots a host fills: an ACCESSOR to read the current identity as
# ``(execution key, fingerprint)`` when a park records it, and a BINDER to bind that identity
# around the out-of-band fire. A resume driver captures and binds through these without importing
# the host, and a host with no identity system leaves them unset — capture yields no identity and
# the fire runs unbound (the pre-registration behavior). ``(execution key, fingerprint)`` are the
# same two strings the durable resume-continuation record stores, so both paths agree.
_ExecutionIdentityAccessor = Callable[[], tuple[str | None, str]]
_ExecutionIdentityBinder = Callable[[str, str], AbstractAsyncContextManager[Any]]

_execution_identity_accessor: _ExecutionIdentityAccessor | None = None
_execution_identity_binder: _ExecutionIdentityBinder | None = None


def register_execution_identity_accessor(accessor: _ExecutionIdentityAccessor) -> None:
    """Register the host accessor that reads the CURRENT execution identity.

    The identity is read as ``(execution key, fingerprint)`` to capture onto a park at park
    time. Last registration wins; a host with none leaves capture yielding ``(None, "")``.
    """
    global _execution_identity_accessor
    _execution_identity_accessor = accessor


def register_execution_identity_binder(binder: _ExecutionIdentityBinder) -> None:
    """Register the host binder that binds an execution identity for the span of an out-of-band fire.

    The binder receives ``(execution key, fingerprint)``. Last registration wins; a host with
    none leaves :func:`bound_execution_identity_for_fire` a no-op (the fire runs unbound).
    """
    global _execution_identity_binder
    _execution_identity_binder = binder


def current_execution_identity() -> tuple[str | None, str]:
    """The current execution identity as ``(execution key, fingerprint)`` for a park to record.

    Returns ``(None, "")`` when no identity is bound or no host registered an accessor (so a
    park under no identity, or on a host without one, records none and its later out-of-band
    fire runs unbound).
    """
    if _execution_identity_accessor is None:
        return None, ""
    return _execution_identity_accessor()


@contextlib.asynccontextmanager
async def bound_execution_identity_for_fire(execution_key: str | None, fingerprint: str) -> AsyncGenerator[None]:
    """Bind ``execution_key`` as the execution identity for an out-of-band fire, via the host binder.

    A no-op that binds NOTHING — the fire runs unbound — when ``execution_key`` is ``None`` (the
    park recorded no identity, taken under none) or no host registered a binder. The binder may
    raise if the key can no longer carry authority; that
    propagates to the fire's own best-effort guard.
    """
    if execution_key is None or _execution_identity_binder is None:
        yield
        return
    async with _execution_identity_binder(execution_key, fingerprint):
        yield


# --- the whole-chain kill teardown seam --------------------------------------------------
#
# A whole-chain kill tears a parked (or running) run down for good: the platform prunes its
# park index and clears its continuation-due record, and each driver that owns durable run state
# tears its OWN state down (its checkpoint, its resolution records, and every run it linked above
# it). The platform holds only registration slots the drivers fill; it FIRES them from
# ``kill_park`` with the run-authorization context bound around the fire (so a handler's
# cross-driver teardown notify authorizes on the killed run's shared delivery identity), and it —
# never a driver — delivers the run's single FAILED afterward.
#
# A handler is fired with ``(interaction_id, reason)`` and returns None. It owns ONLY its driver's
# teardown: a handler that does not own the interaction is a no-op. A handler that cannot finish
# yet (its own park record is not written, a link is still in flight, or a cross-driver teardown
# notify failed) RAISES; the raise propagates so ``kill_park`` keeps the durable kill-due record
# and the reaper redelivers the kill idempotently. What stays best-effort inside a driver's abort
# layer is only the LOCAL finalize of already-dead local state.
_ParkKillHandler = Callable[[str, str], Awaitable[None]]

_park_kill_handlers: list[_ParkKillHandler] = []


def register_park_kill_handler(handler: _ParkKillHandler) -> None:
    """Register a driver teardown fired when a parked/running run is killed whole-chain.

    Every registered handler is fired (in registration order) with the killed
    ``(interaction_id, reason)``; a handler that does not own the interaction is a no-op. A driver
    registers once at import. Handlers accumulate — each driver that resumes parks registers its
    own — so the platform reaches every driver's teardown from one fire.
    """
    _park_kill_handlers.append(handler)


async def fire_park_killed(interaction_id: str, reason: str) -> None:
    """Fire every registered driver teardown for a killed run, in registration order.

    Called by the platform's ``kill_park`` with the run-authorization context already bound. A
    handler that RAISES propagates immediately (a later handler is not fired this pass); the caller
    keeps the durable kill-due record and the reaper redelivers, re-firing every handler — so each
    driver's teardown must be idempotent. With no handler registered this is a no-op (a host with
    no resuming driver has nothing to tear down).
    """
    for handler in _park_kill_handlers:
        await handler(interaction_id, reason)


# The answer value a continuation receives when its interaction expired unanswered — the
# generic marker a resuming consumer reads to run its expiry branch instead of a real answer.
EXPIRY_ANSWER: Final[dict[str, bool]] = {"tai42:interaction_expired": True}


# The shared terminal-status vocabulary a completion fire names its outcome with. The
# resuming driver (which owns the parked run's terminal) and the delivery tool (which maps
# the outcome back to the caller) both key on these SAME two strings, so neither side has to
# name the other to agree on the wire vocabulary — the contract is the single shared point.
#
# The generic completion-fire payload a resuming driver dispatches the bound delivery tool
# with is the opaque bound context merged with the terminal outcome:
#
#     {**bound_context, "result": <terminal outcome>, "completion_id": <str>, "status": <status>}
#
# where ``bound_context`` is the :func:`set_park_completion` context (opaque to the contract),
# ``result`` is the run's terminal outcome value, ``completion_id`` is the stable idempotency
# id of the resolved terminal, and ``status`` is one of the two constants below.
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
# previous ask can refresh it (see :func:`repark_notice`). Only a CHAINED binding is notified
# — a delivery tool that has no horizon of its own is never fired with it — and a re-park
# notice never resolves anything: the completion still fires later under a terminal status.
PARK_COMPLETION_REPARKED: Final[str] = "reparked"


# The ONE reserved field inside an otherwise-opaque completion context: the conversation
# thread the addressed park belongs to. The platform's park-by-thread index reads it (so a
# thread delete can cascade-cancel the parks it would orphan). Everything else in a context
# stays opaque here.
PARK_COMPLETION_THREAD_KEY: Final[str] = "delivery_thread_id"


# The reserved key a platform-produced async-park RESULT carries in place of an answer,
# so a resuming driver recognizes the park by the RESULT shape (never a tool name). The
# in-process client-tool seam stamps it onto the tool result when an async ``ask``
# returns its ``SuspendedInteraction`` sentinel; a resuming driver reads it back off the
# serialized tool output. Generic: it carries the parked interaction id, its deadline, and
# the park's resume OWNER — no driver or engine state.
SUSPENDED_INTERACTION_MARKER_KEY: Final[str] = "tai42:suspended_interaction"


def suspended_interaction_marker(
    interaction_id: str,
    expiry_at: datetime | None,
    resume_owner: str | None = None,
    interaction_ids: list[str] | None = None,
    caller_interaction_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build the reserved marker dict a platform-produced async park returns in place of an answer.

    ``expiry_at`` is rendered ISO-8601 (or ``None``) so the marker
    round-trips through JSON tool-output serialization unchanged.

    ``resume_owner`` is the park's :attr:`SuspendedInteraction.resume_owner`, carried on the
    WIRE FORM so a driver that claims the park off a serialized tool result — never having
    seen the sentinel object — can still check it owns it (:func:`assert_park_adoptable`). It
    defaults to ``None``, which no bound driver may adopt: a marker built without an owner is
    treated as a nested/foreign park rather than silently claimed.

    ``interaction_ids`` are every ask this park represents (defaulting to the single
    ``interaction_id``) and ``caller_interaction_ids`` the subset addressed to the caller
    (``to="caller"``); both ride the wire so a driver reading the marker off a serialized
    tool result can MERGE them when it surfaces a whole super-step at one tool face.

    Disclosure: the ``resume_owner`` field rides the MCP wire wherever a park sentinel is
    serialized, so it exposes an internal resume-tool NAME to the caller. That caller already
    receives the interaction's ``continuation_tool`` on the stored question, so the name is not
    new information to it; the exposure is accepted.
    """
    return {
        SUSPENDED_INTERACTION_MARKER_KEY: {
            "interaction_id": interaction_id,
            "expiry_at": expiry_at.isoformat() if expiry_at is not None else None,
            "resume_owner": resume_owner,
            "interaction_ids": list(interaction_ids) if interaction_ids else [interaction_id],
            "caller_interaction_ids": list(caller_interaction_ids) if caller_interaction_ids else [],
        }
    }


def read_suspended_interaction_marker(content: Any) -> dict[str, Any] | None:
    """Read the reserved park marker back off a tool result, or ``None`` when the result is not a park.

    The tool-output serialization JSON-dumps a dict result to a string, so a
    resuming driver may see the marker as either the live dict or its JSON string;
    both are recognized. A ``str`` that is not JSON, or a JSON value without the
    reserved key, is a normal (non-park) result and yields ``None``. Returns the
    payload — which always carries an ``interaction_id`` and MAY include ``expiry_at``
    and ``resume_owner`` — when a well-formed marker is present.

    The payload is UNTRUSTED: it arrives as tool-result content, which a model can also
    author. The reserved key alone is not enough — the value under it is SHAPE-CHECKED here,
    so a payload that is not a dict, or one carrying no string ``interaction_id`` (``{}``, a
    bare string, an owner-only object a model shaped), yields ``None`` rather than a malformed
    dict a caller would ``KeyError`` on and abort the run over. A park with no valid interaction
    id names nothing to resume, so it is not a park. The returned payload MAY also carry
    ``interaction_ids`` / ``caller_interaction_ids`` (present on a platform-built marker); a
    consumer reading them defaults to the single ``interaction_id`` when they are absent.
    ``resume_owner`` is what makes an otherwise
    well-formed marker checkable — a claimer passes it to :func:`assert_park_adoptable`, and a
    marker carrying no owner (e.g. one a model shaped) names no driver entitled
    to claim it, so it is refused there.
    """
    value: Any = content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, dict) and SUSPENDED_INTERACTION_MARKER_KEY in value:
        # The payload is UNTRUSTED, so guard its real shape here — a non-dict payload, or one with
        # no string ``interaction_id``, is not a park (it would abort a caller on ``KeyError`` at
        # the reserved key).
        marker = cast("object", value[SUSPENDED_INTERACTION_MARKER_KEY])
        if not isinstance(marker, dict):
            return None
        if "interaction_id" not in marker or not isinstance(marker["interaction_id"], str):
            return None
        return cast("dict[str, Any]", marker)
    return None


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
# routing, the seam that converts a returned park sentinel onto the chain key, and the delivery
# tool that reads the fire back.

# Namespace prefix on every chained resume key, so a key naming a CALL is never mistaken for a
# platform interaction id (they share one key space wherever a driver indexes its parks).
CHAINED_PARK_KEY_PREFIX: Final[str] = "tai42:chained-park:"

# The chained resume key, carried under this key in every chain fire's payload: the key the
# CALLER's park is recorded under and the delivery tool reverses back to it.
CHAINED_PARK_TOKEN_KEY: Final[str] = "chain_token"  # noqa: S105 constant identifier, not a secret value


_chained_park_claims: ContextVar[set[str] | None] = ContextVar("tai42_chained_park_claims", default=None)


def new_chained_park_key() -> str:
    """Mint a fresh chained resume key for ONE nested dispatch.

    Namespaced and unique per call, so two nested calls never converge on one key.
    """
    return f"{CHAINED_PARK_KEY_PREFIX}{uuid.uuid4()}"


def is_chained_park_key(key: str) -> bool:
    """Whether ``key`` names a chained CALL rather than a platform interaction.

    The discriminator a driver indexing both key kinds reads before applying any policy that
    only makes sense for one of them (a chained key has no interaction, so nothing expires
    it and no answer is ever delivered against it directly).
    """
    return key.startswith(CHAINED_PARK_KEY_PREFIX)


def _bound_chained_park_key() -> str | None:
    """The chained resume key bound around the CURRENT nested dispatch, or ``None`` if unchained.

    Unchained means no chain routing is bound.
    """
    routing = get_chained_resume()
    return routing.chain_key if routing is not None else None


@contextlib.contextmanager
def chained_park_claims(claims: set[str] | None = None) -> Generator[set[str]]:
    """Bind the per-drive ledger of chained keys :func:`resolve_park_adoption` CLAIMED, yield the set.

    A driver opens one around each drive so it can tell, when the drive stops, which chained
    calls it actually parked on: it drops each key it persists, and whatever REMAINS is a
    claim the drive never turned into a park (the run errored, or moved on without parking).
    Those are dead chains — the nested run will still fire its terminal at them — so the
    driver detaches them rather than leaving the fire to hunt a park that will never exist.

    The yielded set is mutated in place, so a claim recorded inside a task the drive spawns
    (every nested dispatch runs in a child context) is visible to the opener.

    ``claims`` lets a caller pass an EXISTING set to re-bind rather than a fresh one. A driver
    that binds per drive-step — the resume binding cannot span a generator's yields, so it is
    re-entered around each ``__anext__`` — owns one accumulating set for the whole drive and
    re-binds it here each step, so a claim recorded in an early step survives to the reconcile
    at the end. Omit it (the default) to open a fresh ledger for the whole drive at once.
    """
    if claims is None:
        claims = set()
    token = _chained_park_claims.set(claims)
    try:
        yield claims
    finally:
        _chained_park_claims.reset(token)


def attach_chained_park(key: str) -> None:
    """Mark a claimed chained ``key`` as ATTACHED so the ledger drops it.

    The driver recorded a park on it, so it is no longer a dead chain. A no-op when no
    :func:`chained_park_claims` ledger is open, or when the key was never claimed inside it.
    """
    claims = _chained_park_claims.get()
    if claims is not None:
        claims.discard(key)


def resolve_park_adoption(resume_owner: str | None, *, interaction_id: str, tool_name: str) -> tuple[str, str | None]:
    """The park THIS run records for a returned park sentinel — ADOPT-your-own, or CHAIN.

    Returned as the ``(key, resume_owner)`` its own park is keyed and owned by.

    The OBJECT seams' form of :func:`assert_park_adoptable`, for a seam holding the sentinel a
    nested call returned. Three outcomes, in this order:

    * the park is adoptable here (raised under the continuation bound HERE) — it is this run's
      own park, returned unchanged, and the platform resumes it directly;
    * otherwise, if this dispatch was CHAINED (a chain routing was bound around it,
      :func:`get_chained_resume`), the run parks on the chained resume KEY instead, owned by
      this run's OWN resume continuation — because that park is genuinely this run's: it waits
      on the CALL, while the nested run keeps its own park and its own resume and re-enters
      here through the chain's delivery tool when it terminates. The key is recorded in the
      open :func:`chained_park_claims` ledger, so a drive that never turns the claim into a
      park can detach it;
    * otherwise :class:`NestedParkOwnershipError`, exactly as :func:`assert_park_adoptable`
      raises it: waiting on a foreign park with nothing chained would leave this run suspended
      behind a resume fired only at the nested run.

    Never returns a foreign interaction id, and never returns a key owned by anything but the
    continuation bound here — so the claim point downstream reaches the same verdict.
    """
    try:
        assert_park_adoptable(resume_owner, interaction_id=interaction_id, tool_name=tool_name)
    except NestedParkOwnershipError:
        chained = _bound_chained_park_key()
        if chained is None:
            raise
        claims = _chained_park_claims.get()
        if claims is not None:
            claims.add(chained)
        return chained, get_resume_continuation_tool()
    return interaction_id, resume_owner


def repark_notice(expiry_at: datetime | None) -> tuple[str, dict[str, Any]] | None:
    """The ``(tool, payload)`` fired when a park is raised under a bound chain routing.

    Returns ``None`` when no chain routing is bound around the current dispatch.

    A chained caller's own suspension horizon is INHERITED from the nested run's current ask,
    so when that run re-parks on a new ask the caller's horizon must move with it. The notice
    is that signal: the routing's ``delivery_tool`` paired with ``{CHAINED_PARK_TOKEN_KEY:
    routing.chain_key, "expiry_at": <iso>, "status": PARK_COMPLETION_REPARKED}`` — the one
    non-terminal status, carrying the new deadline in place of a result and the chain key the
    delivery tool reverses back to the caller's park. It resolves nothing; the completion still
    fires later under a terminal status.

    Only a bound chain routing is notified, so no other delivery tool ever sees this fire.
    """
    routing = get_chained_resume()
    if routing is None:
        return None
    return routing.delivery_tool, {
        CHAINED_PARK_TOKEN_KEY: routing.chain_key,
        "expiry_at": expiry_at.isoformat() if expiry_at is not None else None,
        "status": PARK_COMPLETION_REPARKED,
    }


def assert_park_adoptable(resume_owner: str | None, *, interaction_id: str, tool_name: str) -> None:
    """Guard the seam where a caller ADOPTS a park as its OWN.

    Applies to a returned sentinel or the wire-form marker, where the caller records resume
    state of its own against that interaction.

    ``resume_owner`` is the park's :attr:`SuspendedInteraction.resume_owner`, and the guard's
    role is a NESTING DISCRIMINATOR, not an authenticity token: it separates a park a run raised
    UNDER ITS OWN resume binding (adoptable) from one that crossed a NESTED run's face on the way
    up (not — that nested run is resumed on its own path, so adopting it would wait for a resume
    fired elsewhere and hang forever). A park a run raises for itself carries the continuation it
    was raised under; a park crossing a nested RUN's face carries no owner at all. So the guard is
    sound on the ``None`` rule, not on name equality: a same-named sibling run is refused because
    its park reaches here ownerless, never because two names happened to differ.

    ``None`` is never adoptable, by ANY caller: it is the signal a park crossed a nested run's
    face (or is an older wire form, or content shaped to look like a park), none of which this
    caller may turn into its own suspended state. An unbound caller is refused too — it has no
    continuation the platform could fire, so a park it claimed could only hang. A BLANK owner
    reads as no owner: it names no registered tool the platform could dispatch.

    This name check does NOT authenticate the marker against any store — the resume owner is a
    public continuation name, so a well-formed marker naming the bound continuation is treated as
    adoptable here. Structural validation of the marker shape lives in
    :func:`read_suspended_interaction_marker`; store-authoritative existence is a separate concern
    the claim seams that hold a store handle apply, not this contract guard.

    Raises :class:`NestedParkOwnershipError` BEFORE any state of the caller's own is recorded,
    so the owning run's park stays untouched and resumable on its own path. A seam holding the
    sentinel a nested CALL returned applies :func:`resolve_park_adoption` instead, which answers
    this same question but can also CHAIN the call rather than refuse it.
    """
    owner = resume_owner if resume_owner and resume_owner.strip() else None
    bound = get_resume_continuation_tool()
    bound = bound if bound and bound.strip() else None
    if owner is not None and owner == bound:
        return
    # The remedy leads (it is what a model reading this can act on); the diagnostic follows. The
    # expected owner is deliberately NOT named — it is a public continuation name, and echoing it
    # would hand a caller the exact string to name for a claim. An ownerless park and an owned-
    # elsewhere park read the same to the caller: this run does not own it.
    detail = (
        "the park crossed a nested run's face carrying no adoptable owner"
        if owner is None
        else "the park was raised under a different run's resume binding"
    )
    # The other way a caller can wait on a park it does not own is to have CHAINED the call
    # (:func:`resolve_park_adoption`). Reaching here means nothing did, so say so — the model's
    # remedy is unchanged, but an operator reading the diagnostics learns which door was shut.
    detail = f"{detail}; nothing chained this call, so this run cannot wait on that outcome"
    raise NestedParkOwnershipError(
        f"Tool {tool_name!r} cannot be used inside this run: it parks on a question another run owns and "
        "resumes, so this run would stay suspended waiting for an outcome delivered elsewhere. Reach that "
        "target through a door that delivers its completion out of band (a conversation route) instead of "
        f"calling it inside this run. [interaction {interaction_id}; {detail}]"
    )


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


def get_park_completion() -> _ParkCompletion:
    """The ``(tool, context)`` a driver fires with a resumed run's FINAL answer.

    The tool delivers the deferred answer and the opaque context it reads to route it. Both are
    ``None`` when no completion delivery is bound (the default, and any code outside a bound
    run).
    """
    return _park_completion.get()


def set_park_completion(tool: str | None = None, context: Mapping[str, Any] | None = None) -> Token[_ParkCompletion]:
    """Bind ``tool`` as the current run's completion continuation and return the reset token.

    ``context`` is an opaque value the delivery tool reads to route the answer. A
    driver calls this around a run whose deferred final answer must be delivered out of
    band; ``context`` is treated as fully opaque here and MUST be JSON-serializable. Pass
    the returned token to :func:`reset_park_completion` to restore the previous value.

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
    return _park_completion.set((tool, context))


def reset_park_completion(token: Token[_ParkCompletion]) -> None:
    """Restore the completion continuation to the value captured in ``token``.

    ``token`` comes from the matching :func:`set_park_completion` call.
    """
    _park_completion.reset(token)
