"""The pending-save operator doors over a stand-in states facet: list, retry, discard.

Each door validates its input loudly, answers the view (never record data or call arguments), and
refuses a save that is not failed with 409 — or 404 when no such save is outstanding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.states.errors import StatesNotConfiguredError
from tai42_contract.states.models import StateSubject

from tai42_skeleton.operations import ConflictError, NotFoundError, NotSupportedError, ValidationRejectedError
from tai42_skeleton.operations import states_outbox as ops
from tai42_skeleton.states.outbox.models import OutboxCall, OutboxRow, OutboxSubject
from tai42_skeleton.states.service.pending_saves import RetryOutcome

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t-1")


def _row(row_id: int, *, status: str = "failed", last_error: str | None = "ValueValidationError: refused") -> OutboxRow:
    return OutboxRow(
        id=row_id,
        status=status,  # type: ignore[arg-type]
        record_keys=[],
        subject_keys=[],
        targets=[],
        states=["profile"],
        run_id="run-1",
        trace_id=None,
        records=[],
        subjects=[
            OutboxSubject(
                state="profile",
                subject=_SUBJECT,
                canonical=_SUBJECT,
                base_seq=None,
                declaration_version=1,
                projected={"secret": "never served"},
            )
        ],
        calls=[OutboxCall(kind="tool", target="notify", payload={"arguments": {"secret": "never served"}})],
        calls_done=0,
        attempts=2,
        next_attempt_at=None,
        claimed_by=None,
        lease_until=None,
        last_error=last_error,
        failed_phase="records" if status == "failed" else None,
        created_at=_CREATED,
        records_applied_at=None,
        failed_at=_CREATED if status == "failed" else None,
    )


class _Facet:
    """The four pending-save methods of the states facet, recording what each door asked for."""

    def __init__(self, *, rows: list[OutboxRow] | None = None, counts: dict[str, int] | None = None) -> None:
        self.rows = rows or []
        self.counts = counts or {}
        self.listed: list[dict[str, Any]] = []
        self.current: OutboxRow | None = None
        self.retry = RetryOutcome(requeued=True, row=None)
        self.discarded: OutboxRow | None = None
        self.discarded_by: list[str | None] = []

    async def list_pending_saves(self, *, status: str | None, limit: int, before: int | None) -> Any:
        self.listed.append({"status": status, "limit": limit, "before": before})
        return self.rows, self.counts

    async def get_pending_save(self, row_id: int) -> OutboxRow | None:
        return self.current

    async def retry_pending_save(self, row_id: int) -> RetryOutcome:
        return self.retry

    async def discard_pending_save(self, row_id: int, *, principal: str | None) -> OutboxRow | None:
        self.discarded_by.append(principal)
        return self.discarded


@pytest.fixture
def facet(monkeypatch: pytest.MonkeyPatch) -> _Facet:
    fake = _Facet()
    monkeypatch.setattr(ops, "_states", lambda: fake)
    return fake


async def test_the_list_serves_the_view_and_the_totals(facet: _Facet) -> None:
    facet.rows = [_row(9), _row(8, status="calls", last_error=None)]
    facet.counts = {"failed": 1, "calls": 3, "pending": 2}
    page = await ops.list_state_pending_saves(status=None, limit=2, cursor="12")
    assert facet.listed == [{"status": None, "limit": 2, "before": 12}]
    assert page["outstanding"] == 6
    assert page["failed"] == 1
    # A full page names the next cursor: the last id served.
    assert page["next_cursor"] == "8"
    first = page["items"][0]
    assert first == {
        "id": "9",
        "status": "failed",
        "run_id": "run-1",
        "states": ["profile"],
        "subjects": [{"state": "profile", "subject": _SUBJECT.model_dump(mode="json")}],
        "calls": [{"kind": "tool", "target": "notify"}],
        "attempts": 2,
        "last_error": "ValueValidationError: refused",
        "failed_phase": "records",
        "created_at": "2026-01-02T03:04:05Z",
        "failed_at": "2026-01-02T03:04:05Z",
    }
    assert "never served" not in str(page)


async def test_a_short_page_is_the_last(facet: _Facet) -> None:
    facet.rows = [_row(3)]
    page = await ops.list_state_pending_saves(status="failed", limit=50)
    assert page["next_cursor"] is None
    assert page["failed"] == 0
    assert facet.listed == [{"status": "failed", "limit": 50, "before": None}]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "held"}, "status is 'outstanding' or 'failed'"),
        ({"limit": 0}, "limit is between 1 and 200"),
        ({"limit": 201}, "limit is between 1 and 200"),
        ({"cursor": "x"}, "a pending save id is an integer"),
        ({"cursor": "0"}, "a pending save id is a positive integer"),
    ],
)
async def test_the_list_refuses_bad_input_loudly(facet: _Facet, kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationRejectedError, match=message):
        await ops.list_state_pending_saves(**kwargs)
    assert facet.listed == []


async def test_the_list_answers_501_when_the_store_is_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Unbound(_Facet):
        async def list_pending_saves(self, *, status: str | None, limit: int, before: int | None) -> Any:
            raise StatesNotConfiguredError("the states component's database is unbound")

    monkeypatch.setattr(ops, "_states", lambda: _Unbound())
    with pytest.raises(NotSupportedError):
        await ops.list_state_pending_saves()


async def test_a_retry_that_applied_answers_applied(facet: _Facet) -> None:
    assert await ops.retry_state_pending_save("7") == {"id": "7", "status": "applied", "last_error": None}


async def test_a_retry_answers_the_status_after_its_records_phase(facet: _Facet) -> None:
    facet.retry = RetryOutcome(requeued=True, row=_row(7, status="failed", last_error="ValueValidationError: again"))
    answer = await ops.retry_state_pending_save("7")
    assert answer == {"id": "7", "status": "failed", "last_error": "ValueValidationError: again"}


async def test_a_retry_of_a_save_that_is_not_failed_is_a_conflict(facet: _Facet) -> None:
    facet.retry = RetryOutcome(requeued=False, row=None)
    facet.current = _row(7, status="calls", last_error=None)
    with pytest.raises(ConflictError, match="pending save 7 is not failed"):
        await ops.retry_state_pending_save("7")


async def test_a_retry_of_an_unknown_save_is_not_found(facet: _Facet) -> None:
    facet.retry = RetryOutcome(requeued=False, row=None)
    with pytest.raises(NotFoundError, match="no pending save 7"):
        await ops.retry_state_pending_save("7")


async def test_a_retry_refuses_an_id_that_is_not_an_integer(facet: _Facet) -> None:
    with pytest.raises(ValidationRejectedError, match="a pending save id is an integer"):
        await ops.retry_state_pending_save("seven")


async def test_a_discard_names_the_discarded_save(facet: _Facet) -> None:
    facet.discarded = _row(5)
    assert await ops.discard_state_pending_save("5") == {"discarded": "5"}
    assert facet.discarded_by == [None]


async def test_a_discard_of_a_save_that_is_not_failed_is_a_conflict(facet: _Facet) -> None:
    facet.current = _row(5, status="running", last_error=None)
    with pytest.raises(ConflictError, match="pending save 5 is not failed"):
        await ops.discard_state_pending_save("5")


async def test_a_discard_of_an_unknown_save_is_not_found(facet: _Facet) -> None:
    with pytest.raises(NotFoundError, match="no pending save 5"):
        await ops.discard_state_pending_save("5")
