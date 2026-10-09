"""The outbox's ordering keys: compact JSON arrays, injective for any key text.

A record key names one record of one state (``[state, tk, tn, kind, key]``); a subject key one
subject across every state (``[tk, tn, kind, key]``); a target key one conversation target
(``[tk, tn]``). The GIN-indexed array columns of ``state_outbox`` hold them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from tai42_contract.states.models import StateSubject, SubjectCandidates


def _key(parts: Iterable[str]) -> str:
    return json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)


def record_key(state: str, subject: StateSubject) -> str:
    """The key of ``subject``'s record under ``state``."""
    return _key((state, subject.target_kind, subject.target_name, subject.kind, subject.key))


def subject_key(target_kind: str, target_name: str, kind: str, key: str) -> str:
    """The key of one subject, whatever the state."""
    return _key((target_kind, target_name, kind, key))


def subject_key_of(subject: StateSubject) -> str:
    """:func:`subject_key` of a :class:`StateSubject`."""
    return subject_key(subject.target_kind, subject.target_name, subject.kind, subject.key)


def target_key(target_kind: str, target_name: str) -> str:
    """The key of one conversation target."""
    return _key((target_kind, target_name))


def subject_keys_of(candidates: SubjectCandidates) -> list[str]:
    """The subject key of every candidate a door resolved, in ``by_kind`` order."""
    return [
        subject_key(candidates.target_kind, candidates.target_name, kind, key)
        for kind, key in candidates.by_kind.items()
    ]


def target_of_key(key: str) -> str:
    """The target key of a record or subject key."""
    parts = json.loads(key)
    head = parts[1:3] if len(parts) == 5 else parts[0:2]
    return _key(head)


def subject_of_record_key(key: str) -> tuple[str, StateSubject]:
    """``(state, subject)`` decoded from a record key."""
    state, tk, tn, kind, subject = json.loads(key)
    return state, StateSubject(target_kind=tk, target_name=tn, kind=kind, key=subject)
