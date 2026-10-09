"""A spy on the run-entry drain: which task queried which subjects, for the per-door proofs.

Every door enters a run through ``visit`` or ``dispatch_scope``, both of which wait for the door's
candidate subjects' pending saves through :func:`~tai42_skeleton.states.outbox.drain.drain_subjects`.
The spy turns the drain on (the rest of the states feature stays as the suite configured it) and
records every drain query — the querying task and the subject keys — without touching a database
(the drain's own behaviour is proven on real Postgres in ``tests/states/outbox``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from tai42_skeleton.states.outbox import drain as drain_mod


@dataclass
class DrainSpy:
    """The drain queries a door made: ``(task, sorted subject keys)`` in order."""

    queries: list[tuple[asyncio.Task[Any] | None, list[str]]] = field(default_factory=list)

    @property
    def keys(self) -> list[list[str]]:
        return [keys for _task, keys in self.queries]

    @property
    def tasks(self) -> list[asyncio.Task[Any] | None]:
        return [task for task, _keys in self.queries]


def spy_on_drains(monkeypatch: pytest.MonkeyPatch) -> DrainSpy:
    """Open the run-entry drain (the states feature on for it alone) and record every query instead of running it."""
    spy = DrainSpy()

    async def _drain(service: Any, keys: Any, deadline: float, *, applying: Any = None) -> None:
        spy.queries.append((asyncio.current_task(), sorted(keys)))

    monkeypatch.setattr(drain_mod, "_states_on", lambda: True)
    monkeypatch.setattr(drain_mod, "drain_subjects", _drain)
    monkeypatch.setattr(drain_mod, "live_states_service", lambda: None)
    return spy


def subject_key(target_kind: str, target_name: str, kind: str, key: str) -> str:
    """The run-entry key of one candidate subject."""
    from tai42_skeleton.states.outbox.keys import subject_key as _subject_key

    return _subject_key(target_kind, target_name, kind, key)
