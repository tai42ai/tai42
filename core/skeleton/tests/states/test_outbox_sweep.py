"""The outbox sweep's lifecycle, offline: start, restart, the epoch's cancel, a failing pass, the gauges, shutdown.

A pass's database work is proven on real Postgres in ``tests/states/outbox``; here the store and the
apply seams are stand-ins, so the loop, the gauges and the shutdown grace are proven on their own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.states.outbox import sweep as sweep_mod
from tai42_skeleton.states.outbox.metrics import outbox_metrics


@pytest.fixture(autouse=True)
def _no_task_left() -> Iterator[None]:
    yield
    task = sweep_mod._sweep_task
    if task is not None:
        task.cancel()
    sweep_mod._sweep_task = None


def _settings(monkeypatch: pytest.MonkeyPatch, *, interval: float = 0.01, grace: float = 0.05) -> None:
    values = SimpleNamespace(outbox_sweep_seconds=interval, outbox_shutdown_grace_seconds=grace)
    monkeypatch.setattr(sweep_mod, "states_settings", lambda: values)
    monkeypatch.setattr(sweep_mod.states_db, "states_store_configured", lambda: True)


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def test_the_sweep_does_not_start_while_the_store_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_mod.states_db, "states_store_configured", lambda: False)
    sweep_mod.start_state_outbox_sweep()
    assert sweep_mod._sweep_task is None


async def test_a_failing_pass_is_logged_and_counted_and_the_loop_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(epoch_mod, "epoch_under_construction_or_none", lambda: None)
    passes: list[int] = []

    async def _pass() -> None:
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("store unreachable")

    monkeypatch.setattr(sweep_mod, "sweep_pass", _pass)
    errors = outbox_metrics().sweep_errors
    before = errors._value.get()
    with caplog.at_level(logging.ERROR, logger=sweep_mod.__name__):
        sweep_mod.start_state_outbox_sweep()
        await _until(lambda: len(passes) >= 3)
    assert errors._value.get() == before + 1
    assert "sweep pass failed" in caplog.text
    await sweep_mod.stop_state_outbox_sweep()
    assert sweep_mod._sweep_task is None


async def test_a_restart_cancels_the_previous_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, interval=60)
    monkeypatch.setattr(epoch_mod, "epoch_under_construction_or_none", lambda: None)
    sweep_mod.start_state_outbox_sweep()
    first = sweep_mod._sweep_task
    assert first is not None
    sweep_mod.start_state_outbox_sweep()
    await asyncio.sleep(0)
    assert first.cancelled()
    assert sweep_mod._sweep_task is not first


async def test_the_epoch_under_construction_cancels_its_own_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, interval=60)
    cancels: list[Callable[[], Awaitable[None]]] = []
    monkeypatch.setattr(
        epoch_mod, "epoch_under_construction_or_none", lambda: SimpleNamespace(register_periodic_loop=cancels.append)
    )
    sweep_mod.start_state_outbox_sweep()
    task = sweep_mod._sweep_task
    assert task is not None
    (cancel,) = cancels
    await cancel()
    assert task.cancelled()
    # A second retire of the same generation finds the task done and does nothing.
    await cancel()


def test_a_sweep_task_that_died_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    died = SimpleNamespace(cancelled=lambda: False, exception=lambda: RuntimeError("boom"))
    cancelled = SimpleNamespace(cancelled=lambda: True, exception=lambda: None)
    with caplog.at_level(logging.ERROR, logger=sweep_mod.__name__):
        sweep_mod._on_sweep_done(cancelled)  # type: ignore[arg-type]
        assert "died" not in caplog.text
        sweep_mod._on_sweep_done(died)  # type: ignore[arg-type]
    assert "the sweep task died unexpectedly" in caplog.text


async def test_shutdown_waits_the_grace_then_cancels_and_names_what_is_left(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _settings(monkeypatch, grace=0.05)
    quick = asyncio.create_task(asyncio.sleep(0), name="tai-states-outbox-quick")
    stuck = asyncio.create_task(asyncio.sleep(30), name="tai-states-outbox-stuck")
    monkeypatch.setattr(sweep_mod, "in_flight_tasks", lambda: {quick, stuck})
    with caplog.at_level(logging.WARNING, logger=sweep_mod.__name__):
        await sweep_mod.stop_state_outbox_sweep()
    assert quick.done()
    assert not quick.cancelled()
    assert stuck.cancelled()
    assert "shutdown cancelled 1 in-flight pending-save task(s)" in caplog.text
    assert "tai-states-outbox-stuck" in caplog.text


async def test_shutdown_with_every_apply_finished_in_the_grace_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _settings(monkeypatch, grace=5)
    quick = asyncio.create_task(asyncio.sleep(0))
    monkeypatch.setattr(sweep_mod, "in_flight_tasks", lambda: {quick})
    with caplog.at_level(logging.WARNING, logger=sweep_mod.__name__):
        await sweep_mod.stop_state_outbox_sweep()
    assert quick.done()
    assert caplog.text == ""


async def test_a_pass_applies_the_due_rows_runs_the_due_calls_and_sets_the_gauges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oldest = datetime.now(UTC) - timedelta(seconds=120)

    class _Store:
        async def outbox_due_pending(self, limit: int) -> list[int]:
            return [1, 2]

        async def outbox_due_calls(self, limit: int) -> list[int]:
            return [3]

        async def outbox_status_counts(self) -> list[tuple[str, int, datetime | None]]:
            return [("failed", 4, oldest), ("calls", 1, datetime.now(UTC))]

    service = SimpleNamespace(_store=_Store())
    applied: list[int] = []
    called: list[int] = []

    async def _apply(svc: Any, row_id: int) -> None:
        applied.append(row_id)

    async def _calls(svc: Any, row_id: int) -> None:
        called.append(row_id)

    monkeypatch.setattr(sweep_mod, "live_states_service", lambda: service)
    monkeypatch.setattr(sweep_mod, "apply_row", _apply)
    monkeypatch.setattr(sweep_mod, "run_calls", _calls)
    await sweep_mod.sweep_pass()
    assert applied == [1, 2]
    assert called == [3]
    metrics = outbox_metrics()
    assert metrics.rows.labels("failed")._value.get() == 4
    assert metrics.rows.labels("pending")._value.get() == 0
    assert metrics.oldest_age_seconds._value.get() >= 120


async def test_a_pass_over_an_empty_outbox_reports_no_age(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Store:
        async def outbox_due_pending(self, limit: int) -> list[int]:
            return []

        async def outbox_due_calls(self, limit: int) -> list[int]:
            return []

        async def outbox_status_counts(self) -> list[tuple[str, int, datetime | None]]:
            return []

    monkeypatch.setattr(sweep_mod, "live_states_service", lambda: SimpleNamespace(_store=_Store()))
    await sweep_mod.sweep_pass()
    assert outbox_metrics().oldest_age_seconds._value.get() == 0.0


def test_the_live_service_is_the_built_apps_and_none_is_built_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app import instance
    from tai42_skeleton.states.outbox import drain as drain_mod

    service = object()
    monkeypatch.setattr(instance, "built_app", lambda: SimpleNamespace(_states_service=service))
    assert drain_mod.live_states_service() is service
    monkeypatch.setattr(instance, "built_app", lambda: None)
    with pytest.raises(RuntimeError, match="the states outbox runs inside the app; no app is built"):
        drain_mod.live_states_service()
