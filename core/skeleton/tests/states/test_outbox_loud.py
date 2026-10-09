"""The loud path of a failed pending save, offline: the operator feed and the platform event.

A feed that cannot take the entry, and an event emit that raises, are each logged and the
failure goes on being reported; nothing is swallowed silently.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from tai42_skeleton.channels import notifications_sink
from tai42_skeleton.hooks import cache as hooks_cache
from tai42_skeleton.interactions import settings as interactions_settings
from tai42_skeleton.states.outbox import apply as apply_mod
from tai42_skeleton.states.outbox import loud
from tai42_skeleton.states.outbox.metrics import outbox_metrics
from tai42_skeleton.states.outbox.models import OutboxRow


def _failed_row() -> OutboxRow:
    return OutboxRow(
        id=11,
        status="failed",
        record_keys=[],
        subject_keys=['["agent","a","thread","t-1"]'],
        targets=[],
        states=["profile"],
        run_id=None,
        trace_id=None,
        records=[],
        subjects=[],
        calls=[],
        calls_done=0,
        attempts=1,
        next_attempt_at=None,
        claimed_by=None,
        lease_until=None,
        last_error="ValueValidationError: refused",
        failed_phase="records",
        created_at=datetime.now(UTC),
        records_applied_at=None,
        failed_at=datetime.now(UTC),
    )


class _Hooks:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(self, *, topic: str, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("hooks store unreachable")
        self.events.append((topic, payload))


@pytest.fixture
def off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply_mod, "on_serving_loop", lambda: False)


async def test_a_failed_save_is_logged_written_to_the_feed_and_emitted(
    monkeypatch: pytest.MonkeyPatch, off_loop: None, caplog: pytest.LogCaptureFixture
) -> None:
    written: list[str] = []

    async def _record(message: str) -> None:
        written.append(message)

    hooks = _Hooks()
    monkeypatch.setattr(interactions_settings, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(notifications_sink, "record_notification", _record)
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    with caplog.at_level(logging.ERROR, logger=loud.__name__):
        await loud.report_failed(_failed_row(), error_kind="validation")
    assert "pending save 11 failed in its records phase (run None" in caplog.text
    assert written == [
        "A pending state save failed; its subjects are held until it is retried or discarded. "
        "Save 11, run -, phase records: ValueValidationError: refused"
    ]
    assert hooks.events == [
        (
            "states_outbox_save_failed",
            {
                "save_id": "11",
                "run_id": None,
                "phase": "records",
                "subject_keys": ['["agent","a","thread","t-1"]'],
                "error_kind": "validation",
                "error": "ValueValidationError: refused",
            },
        )
    ]


async def test_no_feed_configured_is_logged_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(interactions_settings, "interactions_store_configured", lambda: False)
    failures = outbox_metrics().notify_failures
    before = failures._value.get()
    with caplog.at_level(logging.ERROR, logger=loud.__name__):
        await loud.notify_operators("message", save_id=4)
    assert "no notification feed is configured to tell operators about pending save 4" in caplog.text
    assert failures._value.get() == before + 1


async def test_a_feed_write_that_raises_is_logged_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _record(message: str) -> None:
        raise ConnectionError("feed store unreachable")

    monkeypatch.setattr(interactions_settings, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(notifications_sink, "record_notification", _record)
    failures = outbox_metrics().notify_failures
    before = failures._value.get()
    with caplog.at_level(logging.ERROR, logger=loud.__name__):
        await loud.notify_operators("message", save_id=5)
    assert "the operator notification for pending save 5 could not be written" in caplog.text
    assert "feed store unreachable" in caplog.text
    assert failures._value.get() == before + 1


async def test_an_event_emit_that_raises_is_logged(
    monkeypatch: pytest.MonkeyPatch, off_loop: None, caplog: pytest.LogCaptureFixture
) -> None:
    async def _record(message: str) -> None:
        return None

    monkeypatch.setattr(interactions_settings, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(notifications_sink, "record_notification", _record)
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: _Hooks(fail=True))
    with caplog.at_level(logging.WARNING, logger=loud.__name__):
        await loud.report_failed(_failed_row(), error_kind=None)
    assert "failed to emit 'states_outbox_save_failed' for pending save 11" in caplog.text
