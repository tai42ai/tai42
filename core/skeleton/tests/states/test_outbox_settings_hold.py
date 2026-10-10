"""The outbox's deferred-call run and failure record hold no state-store settings across an await.

``run_calls`` and ``_attempt_failed`` run inside root tasks that can outlive the settings
generation that started them (a deferred call runs for as long as its call takes). Each reads
its configuration through the cached accessor at the point of use and keeps only the values it
needs, so a run suspended while a reload retires the settings generation keeps no retired
``StatesSettings`` alive for the stale-settings sweep to report. Each case parks the coroutine at
an await through the REAL cached accessor, retires the generation the way the reload's retire
step does (advance the epoch, reset, sweep), then lets it finish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.settings import reset_all_settings, sweep_stale_settings

from tai42_skeleton.states.outbox import apply as apply_mod


async def _held_while_parked(
    door: Coroutine[Any, Any, Any], gate: asyncio.Future[None], caplog: pytest.LogCaptureFixture
) -> list[Any]:
    """Run ``door`` until it parks on ``gate``, retire the settings generation, sweep, then finish it."""
    task = asyncio.create_task(door)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            held = [h for h in sweep_stale_settings(retired) if h.settings_type.endswith(".StatesSettings")]
    finally:
        gate.set_result(None)
        await task
    return held


@pytest.fixture
async def gate() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()


async def test_run_calls_suspended_in_its_deferred_call_holds_no_retired_settings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None]
) -> None:
    row = SimpleNamespace(id=1, calls=[object()], calls_done=0)
    leases: list[float] = []

    class _Store:
        async def outbox_claim_calls(self, row_id: int, me: str, lease: float) -> Any:
            return ("pending", row)

    async def _call_running(store: Any, claimed: Any, claim: str, lease: float) -> None:
        leases.append(lease)
        await gate

    monkeypatch.setattr(apply_mod, "_run_under_claim", _call_running)
    service = SimpleNamespace(_store=_Store())

    held = await _held_while_parked(apply_mod.run_calls(service, 1), gate, caplog)  # type: ignore[arg-type]

    assert held == []
    assert "StatesSettings" not in caplog.text
    assert leases == [apply_mod.states_settings().outbox_claim_lease_seconds]


async def test_a_failure_record_suspended_on_the_store_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None]
) -> None:
    recorded: list[dict[str, Any]] = []

    class _Store:
        async def outbox_record_failure(self, row_id: int, **kwargs: Any) -> tuple[str, int]:
            recorded.append(kwargs)
            await gate
            return ("pending", 1)

    door = apply_mod._attempt_failed(_Store(), 1, "records", RuntimeError("boom"), claim=None)  # type: ignore[arg-type]

    held = await _held_while_parked(door, gate, caplog)

    assert held == []
    assert "StatesSettings" not in caplog.text
    settings = apply_mod.states_settings()
    assert recorded[0]["max_attempts"] == settings.outbox_max_attempts
    assert recorded[0]["retry_base_seconds"] == settings.outbox_retry_base_seconds
    assert recorded[0]["retry_cap_seconds"] == settings.outbox_retry_cap_seconds
