"""The durable continuation-due record model: the flow-blind hash written when an
async park resolves and read back for redelivery, plus the terminal-drop marker."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import Any, Final

from tai42_contract.states import StateContext


@dataclass(frozen=True)
class ContinuationDue:
    """A durable continuation-due record read back for redelivery — self-contained
    and FLOW-BLIND: a registered tool NAME, the generic ``{interaction_id, answer}``
    the continuation runs with, and the stored execution identity + key fingerprint.
    Carries nothing engine/flow/session specific."""

    interaction_id: str
    tool: str
    identity: str
    fingerprint: str
    answer: Any
    attempts: int
    state_context: StateContext | None = None


class ContinuationRetryDrop(enum.Enum):
    """The sole non-record outcome of ``claim_continuation_retry``: the due member's
    record TTL-expired past its retention horizon and the orphan index member was
    reconciled off — a permanent, terminal drop no further redelivery can ever fire.
    Distinct from ``None`` (not yet / no longer due — a benign no-op)."""

    DROPPED = "dropped"


# The reaper surfaces a loud terminal give-up when the claim returns this.
CONTINUATION_DROPPED: Final = ContinuationRetryDrop.DROPPED


def _continuation_due_mapping(
    tool: str, identity: str, fingerprint: str, answer: Any, state_context: str | None = None
) -> dict[str, str]:
    """The flow-blind continuation-due record fields: a registered tool NAME, the
    stored execution identity + key fingerprint, the generic answer (JSON-encoded so
    any answer shape — a scalar, the expiry sentinel, a form object — round-trips), a
    zeroed attempt count, and the original door's state context (JSON) when the park
    carried one, so a redelivery keeps the same door/actor as the immediate fire.
    Nothing engine/flow/session specific. Written into the SAME MULTI as the resolving
    claim (``record_answer``), so the outbox enqueue commits atomically with the
    ``answered`` state change — a crash can never leave a claimed answer with no
    due-record."""
    mapping = {
        "tool": tool,
        "identity": identity,
        "fingerprint": fingerprint,
        "answer": json.dumps(answer),
        "attempts": "0",
    }
    if state_context is not None:
        mapping["state_context"] = state_context
    return mapping
