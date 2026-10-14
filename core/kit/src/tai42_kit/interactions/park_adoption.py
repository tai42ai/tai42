"""Park adoption and the chained-call notices: which park a run records for a returned sentinel, and what it fires.

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
a CHAIN routing (:class:`~tai42_contract.interactions.ChainedResume`) was bound around parks on
the nested CALL — a key of its own, owned by its own resume continuation, recorded in the open
ledger with the held step's deadline so a drive that never parks on it detaches it sized past that
deadline — while the nested run keeps its park and its resume, and its terminal re-enters the
caller through the routing's delivery tool. A dispatch nothing chained still gets the loud refusal.
The CLAIM point cannot chain: a park reaching it as content belongs to no dispatch it could
attribute the call to, so there it is adopt-or-refuse.

Every driver builds the chain fires here too — :func:`repark_notice` when a chained run re-parks,
:func:`terminal_chain_notice` at its terminal — so each payload has one shape.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Generator
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Final

from tai42_contract.interactions import (
    CHAINED_PARK_KEY_PREFIX,
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_REPARKED,
    PARK_COMPLETION_SUCCEEDED,
    ChainedResume,
    NestedParkOwnershipError,
    RunFailed,
    get_chained_resume,
    get_resume_continuation_tool,
)

__all__ = [
    "PARK_SUPERSEDED_OUTCOME",
    "assert_park_adoptable",
    "attach_chained_park",
    "chained_park_claims",
    "is_chained_park_key",
    "new_chained_park_key",
    "repark_notice",
    "resolve_park_adoption",
    "supersede_chain_notice",
    "terminal_chain_notice",
]

# The person-data-free failed outcome a superseded chain routing is ended with when a later inline
# resumer takes a run over: it names the take-over so an operator trace reads why, and the caller's
# own failure path surfaces it as the uniform "the call came back empty" notice.
PARK_SUPERSEDED_OUTCOME: Final[dict[str, str]] = {
    "status": "superseded",
    "reason": "the run was resumed inline by another caller, which now owns its outcome",
}


_chained_park_claims: ContextVar[dict[str, datetime | None] | None] = ContextVar(
    "tai42_chained_park_claims", default=None
)


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
    """The chained resume key bound around the CURRENT nested dispatch, or ``None`` if unchained."""
    routing = get_chained_resume()
    return routing.chain_key if routing is not None else None


@contextlib.contextmanager
def chained_park_claims(claims: dict[str, datetime | None] | None = None) -> Generator[dict[str, datetime | None]]:
    """Bind the per-drive ledger of chained keys :func:`resolve_park_adoption` CLAIMED, yield the dict.

    A driver opens one around each drive so it can tell, when the drive stops, which chained
    calls it actually parked on: it drops each key it persists, and whatever REMAINS is a
    claim the drive never turned into a park (the run errored, or moved on without parking).
    Those are dead chains — the nested run will still fire its terminal at them — so the
    driver detaches them rather than leaving the fire to hunt a park that will never exist.

    The ledger maps each claimed chained key to the HELD step's deadline — the ``expiry_at``
    :func:`resolve_park_adoption` recorded for it, ``None`` when the held step carried none — so a
    driver sizes the dead-chain detach past that deadline (``expiry_ats=claims.values()``) and the
    tombstone outlasts the run that still fires the key until its own deadline.

    The yielded dict is mutated in place, so a claim recorded inside a task the drive spawns
    (every nested dispatch runs in a child context) is visible to the opener.

    ``claims`` lets a caller pass an EXISTING ledger to re-bind rather than a fresh one. A driver
    that binds per drive-step — the resume binding cannot span a generator's yields, so it is
    re-entered around each ``__anext__`` — owns one accumulating ledger for the whole drive and
    re-binds it here each step, so a claim recorded in an early step survives to the reconcile
    at the end. Omit it (the default) to open a fresh ledger for the whole drive at once.
    """
    if claims is None:
        claims = {}
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
        claims.pop(key, None)


def resolve_park_adoption(
    resume_owner: str | None, *, interaction_id: str, tool_name: str, expiry_at: datetime | None
) -> tuple[str, str | None]:
    """The park THIS run records for a returned park sentinel — ADOPT-your-own, or CHAIN.

    Returned as the ``(key, resume_owner)`` its own park is keyed and owned by.

    The OBJECT seams' form of :func:`assert_park_adoptable`, for a seam holding the sentinel a
    nested call returned. Three outcomes, in this order:

    * the park is adoptable here (raised under the continuation bound HERE) — it is this run's
      own park, returned unchanged, and the platform resumes it directly;
    * otherwise, if this dispatch was CHAINED (a chain routing was bound around it,
      :func:`~tai42_contract.interactions.get_chained_resume`), the run parks on the chained resume
      KEY instead, owned by this run's OWN resume continuation — because that park is genuinely
      this run's: it waits on the CALL, while the nested run keeps its own park and its own resume
      and re-enters here through the chain's delivery tool when it terminates. The key is recorded
      in the open :func:`chained_park_claims` ledger against ``expiry_at`` — the held step's
      deadline — so a drive that never turns the claim into a park detaches it sized past that
      deadline;
    * otherwise :class:`~tai42_contract.interactions.NestedParkOwnershipError`, exactly as
      :func:`assert_park_adoptable` raises it: waiting on a foreign park with nothing chained would
      leave this run suspended behind a resume fired only at the nested run.

    ``expiry_at`` is the returned sentinel's
    :attr:`~tai42_contract.interactions.SuspendedInteraction.expiry_at` — the held step's deadline,
    the latest of its open asks — carried so the ledger can size a dead-chain detach past it. It is
    recorded only on the CHAIN outcome; an adopted own park needs no chain claim and ignores it.

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
            claims[chained] = expiry_at
        return chained, get_resume_continuation_tool()
    return interaction_id, resume_owner


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
    face (or is content shaped to look like a park), neither of which this caller may turn into
    its own suspended state. An unbound caller is refused too — it has no continuation the
    platform could fire, so a park it claimed could only hang. A BLANK owner reads as no owner:
    it names no registered tool the platform could dispatch.

    This name check does NOT authenticate the marker against any store — the resume owner is a
    public continuation name, so a well-formed marker naming the bound continuation is treated as
    adoptable here. Structural validation of the marker shape lives in
    :func:`~tai42_kit.interactions.park_marker.read_suspended_interaction_marker`;
    store-authoritative existence is a separate concern the claim seams that hold a store handle
    apply.

    Raises :class:`~tai42_contract.interactions.NestedParkOwnershipError` BEFORE any state of the
    caller's own is recorded, so the owning run's park stays untouched and resumable on its own
    path. A seam holding the sentinel a nested CALL returned applies :func:`resolve_park_adoption`
    instead, which answers this same question but can also CHAIN the call rather than refuse it.
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


def terminal_chain_notice(routing: ChainedResume, result: Any) -> tuple[str, dict[str, Any]]:
    """The ``(delivery tool, payload)`` a run's terminal fires along its captured chain routing.

    A :class:`~tai42_contract.interactions.RunFailed` result is a failed terminal: its opaque
    ``outcome`` rides as the ``result`` under the ``failed`` status. Any other result is a
    success, carried whole. The payload keys are exactly the chain key, ``result`` and
    ``status``.
    """
    if isinstance(result, RunFailed):
        payload = {
            CHAINED_PARK_TOKEN_KEY: routing.chain_key,
            "result": result.outcome,
            "status": PARK_COMPLETION_FAILED,
        }
    else:
        payload = {CHAINED_PARK_TOKEN_KEY: routing.chain_key, "result": result, "status": PARK_COMPLETION_SUCCEEDED}
    return routing.delivery_tool, payload


def supersede_chain_notice(routing: ChainedResume) -> tuple[str, dict[str, Any]]:
    """The ``(delivery tool, payload)`` that ends a chain routing a later inline resumer superseded.

    A run resumed inline by a caller that chains it with a routing of its own takes the call over:
    the drive runs under the resumer's routing and its terminal reaches the resumer, so the routing
    the park had captured no longer fires at this run's terminal. This notice ends that superseded
    routing ``failed`` with :data:`PARK_SUPERSEDED_OUTCOME`, so a caller still parked on it fails
    loudly through its own error path instead of waiting on a key nothing will fire. The drive still
    fires the resumer's routing with the real terminal through :func:`terminal_chain_notice`.
    """
    return terminal_chain_notice(routing, RunFailed(outcome=dict(PARK_SUPERSEDED_OUTCOME)))
