"""The provider-free, engine-neutral park persist seam and its deadline arithmetic.

:func:`persist_park` writes the durable park index for a suspended super-step — one entry per
interaction plus the super-step barrier — shared by both the LangGraph engines and
``claude_code``. :func:`chained_park_horizon` clamps a chained park's inherited deadline, and
:func:`_gate_expiry_within_retention` refuses a super-step whose ask outlives the retention bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tai42_contract.interactions import attach_chained_park, is_chained_park_key

from tai42_agents._internal.park.capability import ParkIdentity
from tai42_agents._internal.park.errors import ParkExpiryExceedsRetentionError
from tai42_agents._internal.park.index import (
    barrier_ttl_seconds,
    compute_superstep_id,
    persist_superstep,
)
from tai42_agents.settings import agents_limits_settings


def chained_park_horizon(inherited: str | None, retention_bound: datetime | None) -> str:
    """The deadline a CHAINED park is recorded under: the nearest of inherited horizon, cap, and retention bound.

    The inherited horizon comes from the ask its nested run is parked on; the cap is
    configured; the retention bound is this run's.
    Inheritance is the rule — the caller's suspension should last exactly as long as the thing
    it waits on — but a chained park has no ask of its own and no reaper firing at its deadline,
    so an inherited horizon is CLAMPED rather than trusted: the cap bounds how far a nested run
    can pin a suspended caller open, and the retention bound keeps the park inside the window its
    own state is guaranteed to survive. A nested run that carried no deadline at all takes the
    cap, so a chained park is never unbounded.

    Clamped, never refused: unlike an ask deadline (whose gate refuses a park that could not be
    resumed at all), a shortened chained horizon costs nothing — nothing fires AT it, and it only
    sizes how long the index holds the park. A re-park moves it out again.
    """
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=agents_limits_settings().chained_park_horizon_cap_hours)
    if inherited is not None:
        inherited_dt = datetime.fromisoformat(inherited)
        if inherited_dt.tzinfo is None:
            # The inherited deadline crosses the delivery boundary (``deliver_chained_park``'s
            # ``expiry_at``) as an ISO string. Internal producers stamp UTC, but a malformed
            # external fire could omit the offset, and a naive value raises against the tz-aware
            # clamp. Read it as UTC rather than refusing: this horizon only sizes how long the
            # index holds the park (nothing fires AT it), so the "clamped, never refused" rule
            # holds and a cosmetically-off deadline never becomes an endless re-park redelivery.
            inherited_dt = inherited_dt.replace(tzinfo=UTC)
        horizon = min(horizon, inherited_dt)
    if retention_bound is not None:
        horizon = min(horizon, retention_bound)
    return horizon.isoformat()


def _gate_expiry_within_retention(retention_bound: datetime | None, interactions: dict[str, Any]) -> None:
    """Refuse the whole super-step LOUDLY if any parked ask outlives the ``retention_bound``.

    An outliving ask is one with a deadline beyond it, or (under a bounded retention) no
    deadline at all. Refused before a single index key is written, so an unresumable park
    never persists.

    A ``None`` bound (keep-forever) bounds nothing, so every deadline passes.
    """
    if retention_bound is None:
        return
    for interaction_id, expiry in interactions.items():
        if expiry is None:
            raise ParkExpiryExceedsRetentionError(interaction_id, None, retention_bound)
        if datetime.fromisoformat(expiry) > retention_bound:
            raise ParkExpiryExceedsRetentionError(interaction_id, expiry, retention_bound)


async def persist_park(identity: ParkIdentity, parks: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Write the durable park index for a suspended super-step.

    One park entry per suspended interaction plus the super-step barrier all the answers
    converge on. The provider-free, engine-neutral persist seam BOTH the LangGraph engines
    and ``claude_code`` share.

    ``parks`` is every distinct park interrupt of the super-step, each as
    ``(interrupt_id, {interaction_id: expiry})`` — one entry for a single park, many for
    parallel subagent parks. Their interactions form the super-step's union, keyed by the ONE
    super-step id every continuation routes to; each park entry carries the interrupt that ITS
    interaction targets, so the resume feeds each interrupt its own answers. Each entry also
    carries the agent, thread, and the rebuild identity needed to reconstruct the run (any
    engine-specific fact — a LangGraph checkpoint provider / recursion limit — rides inside
    ``rebuild_kwargs``, never a top-level entry field). Keyed by interaction id, so a re-run
    super-step re-parking the same interaction rewrites identically rather than corrupting the
    index. Entries and the barrier are written in ONE MULTI/EXEC (:func:`persist_superstep`),
    all-or-nothing; each entry's TTL is sized to ITS ask's deadline and the barrier's TTL to
    the LATEST deadline, so the barrier expires no earlier than every entry it must outlive.

    Gated up front by :func:`_gate_expiry_within_retention` against ``identity.retention_bound``:
    one ask whose deadline outlives the retention (or lacks one under a bounded retention) fails
    the whole park with zero index state written. A CHAINED key's INHERITED deadline is clamped
    into that bound first (:func:`chained_park_horizon`) rather than failing the park — nothing
    fires at a chained deadline, so a shortened one costs nothing.

    Every key persisted here is marked ATTACHED in the drive's chained-park claims ledger — only
    once the write LANDED, so a failed persist leaves the claim dead and detachable. What remains
    in the ledger when the drive ends is exactly the chains it claimed and never parked on.

    Returns the ``{key: deadline}`` map actually written, so a caller reporting the park (the
    suspended receipt) names the deadlines the index holds rather than the ones proposed.
    """
    interrupt_by_interaction: dict[str, str] = {}
    union: dict[str, Any] = {}
    for interrupt_id, interactions in parks:
        for interaction_id, expiry in interactions.items():
            # A CHAINED key waits on a nested CALL, not on an ask of this run's: its deadline is
            # inherited from what that call is parked on, clamped here (cap + retention) before
            # anything is sized to it. An interaction id keeps its own ask deadline untouched and
            # meets the retention gate below unchanged — a real ask beyond retention still fails
            # the whole park loudly, because nothing could resume it.
            union[interaction_id] = (
                chained_park_horizon(expiry, identity.retention_bound)
                if is_chained_park_key(interaction_id)
                else expiry
            )
            interrupt_by_interaction[interaction_id] = interrupt_id
    _gate_expiry_within_retention(identity.retention_bound, union)
    superstep_id = compute_superstep_id(union.keys())
    entries: dict[str, dict[str, Any]] = {}
    for interaction_id in union:
        entries[interaction_id] = {
            "agent_name": identity.agent_name,
            "thread_id": identity.thread_id,
            "superstep_id": superstep_id,
            # The interrupt THIS interaction's answer targets — its own park interrupt, so a
            # multi-interrupt super-step resumes each interrupt by id.
            "interrupt_id": interrupt_by_interaction[interaction_id],
            "rebuild_kwargs": identity.rebuild_kwargs,
            # The completion tool a clean terminal drive fires with the final answer; carried
            # forward onto every entry so a re-park keeps delivering. ``None`` = no completion
            # (the run face's caller receives the resumed result directly).
            "completion_tool": identity.completion_tool,
            # Its opaque routing context, carried forward the same way (JSON-serializable by
            # contract, so it round-trips this durable entry unchanged).
            "completion_context": dict(identity.completion_context) if identity.completion_context else None,
            # The retention horizon this park was gated against, so a later horizon EXTENSION
            # (a chained park whose nested run re-parked further out) re-clamps against the
            # same bound the persist used instead of guessing one. ``None`` = keep-forever.
            "retention_bound": identity.retention_bound.isoformat() if identity.retention_bound else None,
            # The execution identity this run is authorized as, carried forward onto every entry so
            # the out-of-band abandonment fire binds it and never runs fail-open. ``None`` key = no
            # identity was bound; the fire runs unbound. Present on every entry this code writes —
            # an entry LACKING the key is one persisted before parks recorded it.
            "execution_identity": identity.execution_identity,
            "execution_fingerprint": identity.execution_fingerprint,
        }

    expected = dict(union)
    expiries = {
        interaction_id: (datetime.fromisoformat(expiry) if expiry is not None else None)
        for interaction_id, expiry in union.items()
    }
    await persist_superstep(
        entries, identity.thread_id, superstep_id, expected, expiries, barrier_ttl_seconds(expiries.values())
    )
    for interaction_id in union:
        # Parked on now, so no longer a dead chain the drive must detach. A key the ledger never
        # held (every interaction id, and every park taken with no ledger open) is untouched.
        attach_chained_park(interaction_id)
    return union
