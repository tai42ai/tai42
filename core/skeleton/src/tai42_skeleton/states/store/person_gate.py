"""The person-subject gate: a transaction advisory lock that orders person writes against a person's erase.

Every transaction that writes a record of a ``person`` subject, or a pending save naming one, takes
the gate SHARED for each such subject and then admits the person inside the same transaction. The
erase takes it EXCLUSIVE once, after its erase mark, in a transaction of its own: that returns once
every writer that took the gate before it has committed, and Postgres queues a later shared request
behind it, so that writer admits after the mark and is refused.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from tai42_contract.states.models import PERSON_KIND, StateSubject

from tai42_skeleton.states.outbox.keys import subject_key_of

# Admits the person subjects of a write inside its transaction, after their gates are taken.
PersonAdmission = Callable[[list[StateSubject]], Awaitable[None]]


def person_gate_key(subject: StateSubject) -> str:
    """The advisory-lock key text of one person subject."""
    return "person-subject:" + subject_key_of(subject)


def person_subjects(subjects: Iterable[StateSubject]) -> list[StateSubject]:
    """The distinct ``person`` subjects of ``subjects``, in gate-key order."""
    by_key = {person_gate_key(s): s for s in subjects if s.kind == PERSON_KIND}
    return [by_key[key] for key in sorted(by_key)]


async def lock_person_gates_shared(cur: Any, subjects: Iterable[StateSubject]) -> list[StateSubject]:
    """Take the gate SHARED for every ``person`` subject of ``subjects``; the person subjects, in key order."""
    persons = person_subjects(subjects)
    for subject in persons:
        await cur.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))", (person_gate_key(subject),))
    return persons
