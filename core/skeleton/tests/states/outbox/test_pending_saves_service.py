"""The pending-save operator methods of the states service on real Postgres: list, get, retry, discard."""

from __future__ import annotations

import logging

import pytest

from tai42_skeleton.states.outbox.metrics import outbox_metrics
from tai42_skeleton.states.service import pending_saves as pending_mod

from .conftest import OutboxBed, execute

pytestmark = pytest.mark.integration


@pytest.fixture
def notified(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The operator notifications written, without an interactions store."""
    messages: list[str] = []

    async def _record(message: str) -> None:
        messages.append(message)

    from tai42_skeleton.channels import notifications_sink
    from tai42_skeleton.interactions import settings as interactions_settings

    monkeypatch.setattr(notifications_sink, "record_notification", _record)
    monkeypatch.setattr(interactions_settings, "interactions_store_configured", lambda: True)
    return messages


async def test_the_list_pages_newest_first_and_counts_every_status(bed: OutboxBed) -> None:
    oldest = await bed.enqueue(bed.write(bed.subject("a"), [{"op": "set", "path": ["n"], "value": 1}]))
    middle = await bed.enqueue(bed.write(bed.subject("b"), [{"op": "set", "path": ["n"], "value": 2}]))
    newest = await bed.enqueue(bed.write(bed.subject("c"), [{"op": "set", "path": ["n"], "value": 3}]))
    await bed.fail(middle)

    rows, counts = await bed.svc.list_pending_saves(status=None, limit=2, before=None)
    mine = [row.id for row in rows if bed.state in row.states]
    assert mine == [newest, middle]
    assert counts["failed"] >= 1
    assert counts["pending"] >= 2

    rows, _counts = await bed.svc.list_pending_saves(status=None, limit=50, before=middle)
    assert oldest in [row.id for row in rows]
    assert all(row.id < middle for row in rows)

    rows, _counts = await bed.svc.list_pending_saves(status="failed", limit=50, before=None)
    assert middle in [row.id for row in rows]
    assert all(row.status == "failed" for row in rows)


async def test_the_outstanding_filter_lists_every_save_not_yet_applied_failed_ones_included(bed: OutboxBed) -> None:
    pending = await bed.enqueue(bed.write(bed.subject("a"), [{"op": "set", "path": ["n"], "value": 1}]))
    failed = await bed.enqueue(bed.write(bed.subject("b"), [{"op": "set", "path": ["n"], "value": 2}]))
    await bed.fail(failed)

    rows, _counts = await bed.svc.list_pending_saves(status="outstanding", limit=50, before=None)
    mine = [(row.id, row.status) for row in rows if bed.state in row.states]
    assert mine == [(failed, "failed"), (pending, "pending")]
    unfiltered, _counts = await bed.svc.list_pending_saves(status=None, limit=50, before=None)
    assert [row.id for row in rows] == [row.id for row in unfiltered]


async def test_get_answers_the_save_or_none(bed: OutboxBed) -> None:
    row_id = await bed.enqueue(bed.write(bed.subject(), [{"op": "set", "path": ["n"], "value": 1}]))
    row = await bed.svc.get_pending_save(row_id)
    assert row is not None
    assert row.status == "pending"
    assert row.states == [bed.state]
    await execute("DELETE FROM state_outbox WHERE id = %s", (row_id,))
    assert await bed.svc.get_pending_save(row_id) is None


async def test_a_retry_of_a_save_that_is_not_failed_requeues_nothing(bed: OutboxBed) -> None:
    row_id = await bed.enqueue(bed.write(bed.subject(), [{"op": "set", "path": ["n"], "value": 1}]))
    outcome = await bed.svc.retry_pending_save(row_id)
    assert outcome.requeued is False
    assert outcome.row is None
    assert await bed.status(row_id) == "pending"


async def test_a_discard_drops_a_failed_save_for_good_and_tells_the_operators(
    bed: OutboxBed, notified: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    row_id = await bed.enqueue(bed.write(bed.subject(), [{"op": "set", "path": ["n"], "value": 1}]))
    await bed.fail(row_id)
    discarded = outbox_metrics().discarded
    before = discarded._value.get()
    with caplog.at_level(logging.WARNING, logger=pending_mod.__name__):
        row = await bed.svc.discard_pending_save(row_id, principal="ops-key")
    assert row is not None
    assert row.id == row_id
    assert await bed.status(row_id) is None
    assert discarded._value.get() == before + 1
    assert f"pending save {row_id} discarded by ops-key" in caplog.text
    assert notified == [f"Pending state save {row_id} was discarded; its writes and calls will never be applied."]
    # The record write was never applied.
    assert await bed.svc.read(bed.state, bed.subject()) is None


async def test_a_discard_of_a_save_that_is_not_failed_drops_nothing(bed: OutboxBed, notified: list[str]) -> None:
    row_id = await bed.enqueue(bed.write(bed.subject(), [{"op": "set", "path": ["n"], "value": 1}]))
    assert await bed.svc.discard_pending_save(row_id, principal=None) is None
    assert await bed.status(row_id) == "pending"
    assert notified == []
