"""The durable continuation-due record model.

The flow-blind hash written when an async park resolves and read back for redelivery, plus the
terminal-drop marker and the waiting-outcome record a resumed run's terminal parks for its subject.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.states import StateContext, SubjectCandidates


def subjects_descriptor(candidates: SubjectCandidates) -> dict[str, Any]:
    """The compact subject descriptor denormalized onto a record so a delete path leaves the index.

    Holds the scope ``(target_kind, target_name)`` and the ``by_kind`` map of the subject keys the
    entry joined, so any path that removes the entry can drop it from every
    :meth:`~.keys._StoreKeys.subject_parks_key` set from a SINGLE stored field — without
    re-deriving the subjects from the full request.
    """
    return {
        "target_kind": candidates.target_kind,
        "target_name": candidates.target_name,
        "by_kind": dict(candidates.by_kind),
    }


def iter_subject_keys(descriptor: dict[str, Any]) -> list[tuple[ConversationTargetKind, str, str, str]]:
    """The ``(target_kind, target_name, kind, key)`` tuples a descriptor addresses, one per ``by_kind`` entry."""
    target_kind: ConversationTargetKind = descriptor["target_kind"]
    target_name: str = descriptor["target_name"]
    by_kind: dict[str, str] = descriptor["by_kind"]
    return [(target_kind, target_name, kind, key) for kind, key in by_kind.items()]


@dataclass(frozen=True)
class ContinuationDue:
    """A durable continuation-due record read back for redelivery — self-contained and FLOW-BLIND.

    Carries a registered tool NAME, the generic ``{interaction_id, answer}`` the continuation
    runs with, and the stored execution identity + key fingerprint. It carries no consumer-specific
    data.
    """

    interaction_id: str
    tool: str
    identity: str
    fingerprint: str
    answer: Any
    attempts: int
    state_context: StateContext | None = None
    # The parking run's call chain (``InteractionRequest.asked_by``), so a reaper
    # redelivery restores the same chain the immediate fire did — passed as
    # ``continues_chain`` on the re-entry. Empty when the park carried no chain.
    asked_by: list[str] = field(default_factory=list)
    # The RUN's durable out-of-band address ``{tool, context}`` and its per-run delivery
    # identity, self-contained copies of the interaction's stored fields so the reaper's
    # DETACHED redelivery binds the address and re-establishes the run's delivery identity
    # without re-reading the request. ``None`` for a park that ran under no run-delivery
    # context (a receiver-less door) or a sync question.
    delivery: dict[str, Any] | None = None
    run_delivery_id: str | None = None


class ContinuationRetryDrop(enum.Enum):
    """The sole non-record outcome of ``claim_continuation_retry``.

    The due member's record TTL-expired past its retention horizon and the orphan index member
    was reconciled off — a permanent, terminal drop no further redelivery can ever fire. Distinct
    from ``None`` (not yet / no longer due — a benign no-op).
    """

    DROPPED = "dropped"


# The reaper surfaces a loud terminal give-up when the claim returns this.
CONTINUATION_DROPPED: Final = ContinuationRetryDrop.DROPPED


class KillRetryDrop(enum.Enum):
    """The sole non-record outcome of ``claim_kill_retry`` when the kill-due record hash is gone.

    An orphan index member whose kill-due record TTL-expired (a backstop past the give-up
    deadline): reconciled off the index, carrying no fields — distinct from ``None`` (not yet / no
    longer due). The reaper's ordinary give-up reads the record's ``deadline_ms`` while it still
    exists; this marker is only the ancient-orphan reconcile.
    """

    DROPPED = "dropped"


KILL_DROPPED: Final = KillRetryDrop.DROPPED


def _continuation_due_mapping(
    tool: str,
    identity: str,
    fingerprint: str,
    answer: Any,
    state_context: str | None = None,
    asked_by: list[str] | None = None,
    delivery: str | None = None,
    run_delivery_id: str | None = None,
) -> dict[str, str]:
    """The flow-blind continuation-due record fields.

    Carries a registered tool NAME, the stored execution identity + key fingerprint, the generic
    answer (JSON-encoded so any answer shape — a scalar, the expiry sentinel, a form object —
    round-trips), a zeroed attempt count, and the original door's state context (JSON) when the
    park carried one, so a redelivery keeps the same door/actor as the immediate fire. It also
    copies the parking run's call chain (``asked_by``, JSON) so a redelivery restores the same
    chain the immediate fire did, and the run's ``delivery`` address (JSON) and ``run_delivery_id``
    when the park carried them, so the reaper's DETACHED redelivery binds the run's address and
    re-establishes its delivery identity without re-reading the request. It carries no
    consumer-specific data. Written into the SAME MULTI as the resolving claim
    (``record_answer``), so the outbox enqueue commits atomically with the ``answered`` state
    change — a crash can never leave a claimed answer with no due-record.
    """
    mapping = {
        "tool": tool,
        "identity": identity,
        "fingerprint": fingerprint,
        "answer": json.dumps(answer),
        "attempts": "0",
    }
    if state_context is not None:
        mapping["state_context"] = state_context
    if asked_by:
        mapping["asked_by"] = json.dumps(asked_by)
    if delivery is not None:
        mapping["delivery"] = delivery
    if run_delivery_id is not None:
        mapping["run_delivery_id"] = run_delivery_id
    return mapping


@dataclass(frozen=True)
class KillTarget:
    """What a whole-chain kill reads off the surviving record to tear a run down.

    ``kind`` is ``"park"`` (a pending ask), ``"running"`` (an answered/resuming entry — its state
    hash retained or only its continuation-due record left), or ``"outcome"`` (a completed run's
    waiting result). ``group_id`` is the pending park's group (for the prune's count cleanup),
    ``None`` for the other kinds. ``delivery`` (the run's out-of-band address ``{tool, context}``),
    ``run_delivery_id`` and ``subjects`` (the descriptor) are the run's delivery identity the kill
    copies onto its durable record and delivers its single FAILED under — ``None`` when the run
    carried none (a receiver-less door, no subject).
    """

    kind: Literal["park", "running", "outcome"]
    group_id: str | None
    delivery: dict[str, Any] | None
    run_delivery_id: str | None
    subjects: dict[str, Any] | None


@dataclass(frozen=True)
class KillDue:
    """A durable kill-due record read back for redelivery — the whole-chain kill's outbox.

    Written in the kill's teardown MULTI and cleared only when every driver teardown returned
    normally AND the platform's FAILED delivery for the killed run committed. Carries the killed
    run's OWN copied ``delivery`` address and ``run_delivery_id`` (so the FAILED delivery and its
    dedup id survive the prune and a redelivery), the subject descriptor it subject-tracks a
    receiver-less FAILED under, the removal ``reason``, and the hard ``deadline_ms`` past which the
    reaper gives the kill up. It carries no consumer-specific data.
    """

    interaction_id: str
    reason: str
    attempts: int
    deadline_ms: int
    delivery: dict[str, Any] | None = None
    run_delivery_id: str | None = None
    subjects: dict[str, Any] | None = None


def _kill_due_mapping(
    reason: str,
    deadline_ms: int,
    delivery: str | None = None,
    run_delivery_id: str | None = None,
    subjects: str | None = None,
) -> dict[str, str]:
    """The kill-due record fields.

    The removal ``reason``, a zeroed attempt count, the hard ``deadline_ms`` (created + retention
    horizon) past which the reaper gives the kill up, and — when the killed run carried them — the
    run's ``delivery`` address (JSON), its ``run_delivery_id`` and its subject descriptor (JSON),
    so a detached redelivery re-fires the driver teardown and delivers the run's single FAILED
    without re-reading a state hash the kill already pruned. Written into the kill's teardown MULTI,
    so the outbox enqueue commits atomically with the prune.
    """
    mapping = {"reason": reason, "attempts": "0", "deadline_ms": str(deadline_ms)}
    if delivery is not None:
        mapping["delivery"] = delivery
    if run_delivery_id is not None:
        mapping["run_delivery_id"] = run_delivery_id
    if subjects is not None:
        mapping["subjects"] = subjects
    return mapping


@dataclass(frozen=True)
class WaitingOutcome:
    """A resumed run's terminal, parked for its subject — read back by whoever owns the subject.

    Keyed by the run's ``completion_id``. ``status`` is ``"finished"`` (the run succeeded) or
    ``"failed"`` (a give-up / kill); ``result`` is the terminal payload — the succeeded outcome
    for ``finished`` or the error for ``failed``. ``subjects`` is the descriptor the row is indexed
    under, so a take or an erasure drops the ``completion_id`` from every subject-parks set it
    joined. ``interaction_id`` (the resumed interaction whose terminal wrote the row) and
    ``run_delivery_id`` (the run's delivery identity) ride the row so the retention sweep's
    ``interactions_outcome_dropped_untaken`` event names the run. It carries no consumer-specific data.
    """

    completion_id: str
    status: Literal["finished", "failed"]
    result: Any
    subjects: dict[str, Any] | None = None
    interaction_id: str | None = None
    run_delivery_id: str | None = None


def _outcome_mapping(
    completion_id: str,
    status: Literal["finished", "failed"],
    result: Any,
    subjects: dict[str, Any],
    created_at_ms: int,
    interaction_id: str,
    run_delivery_id: str | None,
) -> dict[str, str]:
    """The waiting-outcome record fields.

    The per-run ``completion_id`` (the dedup key), the ``finished``/``failed`` status, the terminal
    ``result`` (JSON — the succeeded outcome or the error), the subject descriptor (JSON) the row is
    indexed under, the creation time (ms) the retention sweep ages the row against, the resumed
    ``interaction_id``, and the run's ``run_delivery_id`` when it carried one — the last two so the
    sweep's dropped-untaken event names the run that was dropped.
    """
    mapping = {
        "completion_id": completion_id,
        "status": status,
        "result": json.dumps(result),
        "subjects": json.dumps(subjects),
        "created_at_ms": str(created_at_ms),
        "interaction_id": interaction_id,
    }
    if run_delivery_id is not None:
        mapping["run_delivery_id"] = run_delivery_id
    return mapping
