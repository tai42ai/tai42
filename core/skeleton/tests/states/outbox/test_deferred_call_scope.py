"""Inside a deferred call, on real Postgres: a drive beneath it skips exactly the saves whose calls wait on its save.

The call's own save and every save its claim holds behind it are skipped (waiting on them would be
waiting on itself); every other save is waited for or raised on as anywhere else; a task that
outlives the call, or a scope whose claim lapsed, skips nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from tai42_contract.states.errors import StatePendingSaveFailedError
from tai42_contract.states.models import StateContext, SubjectCandidates

from tai42_skeleton.states.context import state_context
from tai42_skeleton.states.outbox.drain import current_applying_save, outbox_apply_scope, run_entry_drain
from tai42_skeleton.states.outbox.keys import subject_key

from .conftest import OutboxBed, execute

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


def _key(name: str) -> str:
    return subject_key("agent", "a", "thread", name)


# -- inside a deferred call ----------------------------------------------------------------------------


async def _running_save(bed: OutboxBed, subject: str, claim: str = "me") -> int:
    with _door(subject):
        row = await bed.enqueue(calls=(("echo", {}),))
    await execute("UPDATE state_outbox SET status = 'running', claimed_by = %s WHERE id = %s", (claim, row))
    return row


async def test_a_child_of_a_deferred_call_skips_its_own_save_and_the_saves_waiting_on_it(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    monkeypatch.setenv("STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("STATES_OUTBOX_DRAIN_POLL_SECONDS", "0.05")
    saving = await _running_save(bed, "A")
    with _door("A"):
        newer = await bed.enqueue(calls=(("echo", {}),))  # held behind ``saving`` by the claim
    ran: list[bool] = []

    async def _child() -> None:
        with _door("A"):
            async with run_entry_drain():
                ran.append(True)

    with outbox_apply_scope(saving, frozenset({_key("A")}), "me"):
        await asyncio.create_task(_child())
        await asyncio.gather(_child())
    assert ran == [True, True]
    assert await bed.status(newer) == "calls"  # skipped, never run by the child


async def test_a_child_of_a_deferred_call_waits_for_an_older_save_on_another_subject(
    bed: OutboxBed, probe: Any
) -> None:
    older = await bed.enqueue(bed.write(bed.subject("C"), [_set("n", 1)]))
    saving = await _running_save(bed, "A")
    seen: list[Any] = []

    async def _child() -> None:
        with _door("C"):
            async with run_entry_drain():
                seen.append(await bed.svc.read(bed.state, bed.subject("C")))

    with outbox_apply_scope(saving, frozenset({_key("A")}), "me"):
        await asyncio.create_task(_child())
    assert await bed.status(older) is None
    assert seen[0].data == {"n": 1}


async def test_a_child_of_a_deferred_call_applies_and_waits_for_a_newer_unrelated_save(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    saving = await _running_save(bed, "A")
    with _door("C"):
        newer = await bed.enqueue(bed.write(bed.subject("C"), [_set("n", 7)]), calls=(("probe", {}),))
    finished: list[str | None] = []

    async def _finish_newer_calls() -> None:
        while await bed.status(newer) != "calls":
            await asyncio.sleep(0.02)
        await execute("DELETE FROM state_outbox WHERE id = %s", (newer,))

    async def _child() -> None:
        with _door("C"):
            async with run_entry_drain():
                finished.append(await bed.status(newer))

    finisher = asyncio.create_task(_finish_newer_calls())
    with outbox_apply_scope(saving, frozenset({_key("A")}), "me"):
        await asyncio.create_task(_child())
    await finisher
    assert finished == [None]  # its records applied and its calls finished before the child's body
    record = await bed.svc.read(bed.state, bed.subject("C"))
    assert record is not None
    assert record.data == {"n": 7}


async def test_a_child_of_a_deferred_call_raises_on_a_newer_failed_save(bed: OutboxBed, probe: Any) -> None:
    saving = await _running_save(bed, "A")
    failed = await bed.enqueue(bed.write(bed.subject("C"), [_set("n", 1)]))
    await bed.fail(failed)

    async def _child() -> None:
        with _door("C"):
            async with run_entry_drain():
                pytest.fail("never runs on a held subject")

    with outbox_apply_scope(saving, frozenset({_key("A")}), "me"), pytest.raises(StatePendingSaveFailedError) as raised:
        await asyncio.create_task(_child())
    assert raised.value.extra == {"save_id": str(failed)}


async def test_a_child_of_a_deferred_call_skips_a_save_whose_chain_ends_at_it(
    bed: OutboxBed, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    monkeypatch.setenv("STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("STATES_OUTBOX_DRAIN_POLL_SECONDS", "0.05")
    saving = await _running_save(bed, "A")
    multi = StateContext(
        door="api",
        candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "A", "x": "D"}),
    )
    with state_context(multi):
        await bed.enqueue(calls=(("echo", {}),))  # Q on A and D: waits on ``saving`` directly
    chain = StateContext(
        door="api",
        candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"thread": "C", "x": "D"}),
    )
    with state_context(chain):
        r = await bed.enqueue(calls=(("echo", {}),))  # R on C and D: its blocker Q waits on ``saving``
    ran: list[bool] = []

    async def _child() -> None:
        with _door("C"):
            async with run_entry_drain():
                ran.append(True)

    with outbox_apply_scope(saving, frozenset({_key("A")}), "me"):
        await asyncio.create_task(_child())
    assert ran == [True]
    assert await bed.status(r) == "calls"


@pytest.mark.parametrize("lapse", ["deleted", "taken_over"])
async def test_a_task_that_outlives_its_deferred_call_ignores_the_inherited_scope(
    bed: OutboxBed, probe: Any, lapse: str
) -> None:
    saving = await _running_save(bed, "A")
    newer = await bed.enqueue(bed.write(bed.subject("A"), [_set("n", 3)]))
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str | None] = []

    async def _outliving_child() -> None:
        started.set()
        await release.wait()
        with _door("A"):
            async with run_entry_drain():
                seen.append(await bed.status(newer))

    with outbox_apply_scope(saving, frozenset({_key("A")}), "me"):
        child = asyncio.create_task(_outliving_child())
        await started.wait()
    assert current_applying_save() is None  # the scope ended with the call
    finisher = None
    if lapse == "deleted":
        await execute("DELETE FROM state_outbox WHERE id = %s", (saving,))
    else:
        # Another runner took the save over; the child now waits for it like any running save.
        await execute("UPDATE state_outbox SET claimed_by = 'another' WHERE id = %s", (saving,))

        async def _other_runner_finishes() -> None:
            await asyncio.sleep(0.2)
            await execute("DELETE FROM state_outbox WHERE id = %s", (saving,))

        finisher = asyncio.create_task(_other_runner_finishes())
    release.set()
    await child
    if finisher is not None:
        await finisher
    assert seen == [None]  # the newer save on A was applied first: no skip from a dead scope
