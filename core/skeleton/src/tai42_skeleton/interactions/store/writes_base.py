"""Addressing and subject-index write primitives shared by the question-lifecycle mutations.

The open-slot index selector, the denormalized ``to`` reader, and the subject-parks leave enqueuer
that the add, answer, prune and kill mutations all write through.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from . import records
from .keys import _StoreKeys

# Which door a park's answer/outcome is addressed to. ``"user"`` asks reserve from
# ``open_key`` under ``max_concurrent``; ``"caller"`` asks (and the waiting outcomes
# they leave) reserve from ``open_caller_key`` under ``max_concurrent_caller``. The two
# caps are independent. Denormalized onto the state hash (absent means ``"user"``) so a
# terminal claim / prune frees the RIGHT index member from a single ``hget``.
AskTo = Literal["user", "caller"]


class _StoreWritesBase(_StoreKeys):
    """The addressing and subject-leave write helpers shared by every question-lifecycle mutation."""

    def _open_key_for(self, to: AskTo) -> str:
        """The open-slot ZSET a ``to``-addressed entry reserves from — the two caps are independent."""
        return self.open_caller_key if to == "caller" else self.open_key

    @staticmethod
    def _addressed_to(value: str | None) -> AskTo:
        """Read the denormalized ``to`` field: ``"caller"`` only when explicitly stamped, else ``"user"``."""
        return "caller" if value == "caller" else "user"

    def _queue_subject_leave(self, pipe: Any, subjects: str | None, member: str) -> None:
        """Enqueue the ``SREM`` of ``member`` from every subject-parks set the descriptor addresses."""
        if subjects is None:
            return
        for target_kind, target_name, kind, key in records.iter_subject_keys(json.loads(subjects)):
            pipe.srem(self.subject_parks_key(target_kind, target_name, kind, key), member)
