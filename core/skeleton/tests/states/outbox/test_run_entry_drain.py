"""The run-entry drain on real Postgres: the skip rule, held subjects, waiting for calls, and the deferred-call scope.

A synthetic door: a hand-built :class:`StateContext` whose candidates name the subject, entered
through :func:`run_entry_drain` exactly as ``visit`` and ``dispatch_scope`` enter it. The query
the drain runs per entry (``outbox_outstanding_on_subjects``) is counted by a spy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from tai42_contract.states.errors import StatePendingSaveFailedError, StatePendingSaveTimeoutError
from tai42_contract.states.models import StateContext, SubjectCandidates

from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.states.context import state_context
from tai42_skeleton.states.outbox.drain import run_entry_drain
from tai42_skeleton.states.outbox.keys import subject_key

from .conftest import OutboxBed, execute, set_states_env

pytestmark = pytest.mark.integration


def _set(field: str, value: Any) -> dict[str, Any]:
    return {"op": "set", "path": [field], "value": value}


def _ctx(key: str) -> StateContext:
    return StateContext(
        door="api", candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": key})
    )


@contextmanager
def _door(key: str) -> Iterator[None]:
    with state_context(_ctx(key)):
        yield


@pytest.fixture
def queries(bed: OutboxBed, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every subject-key set a drain queried, in order."""
    seen: list[list[str]] = []
    real = bed.svc._store.outbox_outstanding_on_subjects

    async def spy(keys: Any) -> Any:
        seen.append(sorted(keys))
        return await real(keys)

    monkeypatch.setattr(bed.svc._store, "outbox_outstanding_on_subjects", spy)
    return seen


def _key(name: str) -> str:
    return subject_key("agent", "a", "thread", name)


async def test_a_nested_entry_on_the_same_task_and_subject_runs_no_query(bed: OutboxBed, queries: list) -> None:
    with _door("A"):
        async with run_entry_drain(), run_entry_drain():
            pass
    assert queries == [[_key("A")]]


async def test_a_nested_entry_naming_a_new_subject_drains_only_that_subject(bed: OutboxBed, queries: list) -> None:
    with _door("A"):
        async with run_entry_drain():
            with _door("B"):
                async with run_entry_drain():
                    pass
    assert queries == [[_key("A")], [_key("B")]]


async def test_a_child_task_drains_its_own_subject_before_its_body(bed: OutboxBed, queries: list) -> None:
    row = await bed.enqueue(bed.write(bed.subject("B"), [_set("n", 1)]))
    seen: list[str | None] = []

    async def _child() -> None:
        with _door("B"):
            async with run_entry_drain():
                seen.append(await bed.status(row))

    with _door("A"):
        async with run_entry_drain():
            await asyncio.create_task(_child())
    assert seen == [None]  # the save on B landed before the child's body ran
    assert queries == [[_key("A")], [_key("B")]]


async def test_a_child_task_on_the_parents_own_subject_re_checks_it(bed: OutboxBed, queries: list) -> None:
    async def _child() -> None:
        async with run_entry_drain():
            pass

    with _door("A"):
        async with run_entry_drain():
            await asyncio.create_task(_child())
            await asyncio.gather(_child(), _child())
    assert queries == [[_key("A")]] * 4  # the parent once, the created task and each gathered task once


async def test_a_root_task_spawned_inside_an_entry_drains_again(bed: OutboxBed, queries: list) -> None:
    async def _root() -> None:
        with _door("A"):
            async with run_entry_drain():
                pass

    with _door("A"):
        async with run_entry_drain():
            await spawn_root_task(_root())
    assert queries == [[_key("A")], [_key("A")]]


async def test_a_door_with_nothing_forwarded_drains_nothing(bed: OutboxBed, queries: list) -> None:
    async with run_entry_drain():
        pass
    assert queries == []


async def test_the_entry_on_a_subject_held_behind_a_failed_save_raises_at_once(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS="0.3", STATES_OUTBOX_DRAIN_POLL_SECONDS="0.05")
    failed, _held = await bed.enqueue_together(
        [bed.write(bed.subject("A"), [_set("n", 1)])],
        [bed.write(bed.subject("A"), [_set("n", 2)]), bed.write(bed.subject("B"), [_set("n", 3)])],
    )
    await bed.fail(failed)
    with _door("B"), pytest.raises(StatePendingSaveFailedError) as raised:
        async with run_entry_drain():
            pytest.fail("the body must not run on a held subject")
    assert raised.value.extra == {"save_id": str(failed)}
    assert f"held behind failed pending save {failed}" in str(raised.value)


async def test_the_entry_on_a_calls_row_behind_a_failed_row_raises_at_once(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS="0.3", STATES_OUTBOX_DRAIN_POLL_SECONDS="0.05")
    # A failed save on subject A; then a calls row whose subjects are B and A.
    failed = await bed.enqueue(bed.write(bed.subject("A"), [_set("n", 1)]))
    await bed.fail(failed)
    with state_context(
        StateContext(
            door="api",
            candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "B", "person": "x"}),
        )
    ):
        calls_row = await bed.enqueue(calls=(("echo", {}),))
    await execute(
        "UPDATE state_outbox SET subject_keys = subject_keys || %s::text[] WHERE id = %s", ([_key("A")], calls_row)
    )
    with _door("B"), pytest.raises(StatePendingSaveFailedError) as raised:
        async with run_entry_drain():
            pass
    assert raised.value.extra == {"save_id": str(failed)}


async def test_the_entry_waits_for_a_running_call_and_proceeds_when_it_is_deleted(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_POLL_SECONDS="0.02")
    with _door("A"):
        row = await bed.enqueue(calls=(("echo", {}),))
    await execute("UPDATE state_outbox SET status = 'running', claimed_by = 'other' WHERE id = %s", (row,))
    entered = asyncio.Event()

    async def _enter() -> None:
        with _door("A"):
            async with run_entry_drain():
                entered.set()

    waiter = asyncio.create_task(_enter())
    await asyncio.sleep(0.2)
    assert not entered.is_set()
    await execute("DELETE FROM state_outbox WHERE id = %s", (row,))
    await asyncio.wait_for(waiter, 5)
    assert entered.is_set()


async def test_a_running_call_past_the_deadline_times_out_naming_it(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    set_states_env(monkeypatch, STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS="0.3", STATES_OUTBOX_DRAIN_POLL_SECONDS="0.05")
    with _door("A"):
        row = await bed.enqueue(calls=(("echo", {}),))
    await execute("UPDATE state_outbox SET status = 'running', claimed_by = 'other' WHERE id = %s", (row,))
    with _door("A"), pytest.raises(StatePendingSaveTimeoutError, match=f"still has pending save {row} after 0.3s"):
        async with run_entry_drain():
            pass
