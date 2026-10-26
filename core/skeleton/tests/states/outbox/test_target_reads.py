"""The reads keyed by conversation target on real Postgres: outstanding saves and record counts."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.states.outbox.keys import target_key

from .conftest import OutboxBed

pytestmark = pytest.mark.integration


def _set(value: int) -> list[dict[str, Any]]:
    return [{"op": "set", "path": ["n"], "value": value}]


async def test_the_outstanding_saves_under_targets_are_the_rows_holding_any_asked_target(bed: OutboxBed) -> None:
    held, other, unasked = f"H{bed.state}", f"O{bed.state}", f"U{bed.state}"
    pending = await bed.enqueue(bed.write(bed.subject("s1", target_kind="tool", target_name=held), _set(1)))
    failed = await bed.enqueue(bed.write(bed.subject("s2", target_kind="tool", target_name=held), _set(2)))
    await bed.fail(failed)
    on_other = await bed.enqueue(bed.write(bed.subject("s3", target_kind="tool", target_name=other), _set(3)))
    await bed.enqueue(bed.write(bed.subject("s4", target_kind="tool", target_name=unasked), _set(4)))
    await bed.enqueue(bed.write(bed.subject("s5", target_kind="agent", target_name=held), _set(5)))

    rows = await bed.svc._store.outbox_outstanding_on_targets([target_key("tool", held), target_key("tool", other)])

    assert [(row_id, status) for row_id, status, _keys, _targets in rows] == [
        (pending, "pending"),
        (failed, "failed"),
        (on_other, "pending"),
    ]
    assert [targets for _id, _status, _keys, targets in rows] == [
        [target_key("tool", held)],
        [target_key("tool", held)],
        [target_key("tool", other)],
    ]
    assert all(len(keys) == 1 for _id, _status, keys, _targets in rows)


async def test_no_asked_target_reads_no_outstanding_save(bed: OutboxBed) -> None:
    await bed.enqueue(bed.write(bed.subject("s1", target_kind="tool", target_name=f"H{bed.state}"), _set(1)))
    assert await bed.svc._store.outbox_outstanding_on_targets([target_key("tool", f"none{bed.state}")]) == []


async def test_the_record_counts_under_targets_name_only_the_asked_targets_holding_records(bed: OutboxBed) -> None:
    two, one, empty = f"A{bed.state}", f"B{bed.state}", f"C{bed.state}"
    for target, key in ((two, "s1"), (two, "s2"), (one, "s3"), (f"D{bed.state}", "s4")):
        await bed.enqueue(bed.write(bed.subject(key, target_kind="tool", target_name=target), _set(1)))
    await bed.enqueue(bed.write(bed.subject("s5", target_kind="agent", target_name=one), _set(1)))
    for target in (two, one, f"D{bed.state}"):
        assert await bed.svc.drain_target_saves("tool", target) == []
    assert await bed.svc.drain_target_saves("agent", one) == []

    counts = await bed.svc._store.count_records_for_targets("tool", [two, one, empty])

    assert counts == {two: 2, one: 1}
