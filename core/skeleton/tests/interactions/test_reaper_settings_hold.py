"""The interactions reaper passes and ``kill_park`` hold no settings instance across an await.

The reaper is a perpetual loop that outlives a settings reload. Each pass (and the
whole-chain kill a pass runs per due park) reads its configuration through the
cached accessor at the point of use, so a pass suspended on the store while the
reload retires the settings generation keeps no retired ``InteractionsSettings``
alive for the stale-settings sweep to report. Each case parks one door at its
first store await through the REAL cached accessor, retires the generation the
way the reload's retire step does (advance the epoch, reset, sweep), then lets
the door finish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.settings import reset_all_settings, sweep_stale_settings

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import kill as kill_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import KillTarget

from ._continuation_support import configure_interactions_store


async def _held_while_parked(
    door: Callable[[], Coroutine[Any, Any, Any]],
    gate: asyncio.Future[None],
    caplog: pytest.LogCaptureFixture,
) -> list[Any]:
    """Run ``door`` until it parks on ``gate``, retire the settings generation, sweep, then finish it."""
    task = asyncio.create_task(door())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done()
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            held = [h for h in sweep_stale_settings(retired) if h.settings_type.endswith(".InteractionsSettings")]
    finally:
        gate.set_result(None)
        await task
    return held


@pytest.fixture
async def gate() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()


@pytest.fixture
def real_accessor(monkeypatch: pytest.MonkeyPatch, fake_client_ctx) -> None:
    """Configure the store and read settings through the real cached (epoch-stamped) accessor."""
    configure_interactions_store(monkeypatch)
    interactions_settings.cache_clear()
    monkeypatch.setattr(reaper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(kill_module, "client_ctx", fake_client_ctx)


@pytest.mark.usefixtures("real_accessor")
@pytest.mark.parametrize(
    ("door", "first_store_await"),
    [
        (reaper_module.reap_expired_parks_once, "due_expiries"),
        (reaper_module.redeliver_due_continuations_once, "due_continuations"),
        (reaper_module.redeliver_due_kills_once, "due_kills"),
        (reaper_module.sweep_untaken_outcomes_once, "due_untaken_outcomes"),
    ],
    ids=["expired_parks", "due_continuations", "due_kills", "untaken_outcomes"],
)
async def test_a_reaper_pass_suspended_on_the_store_holds_no_retired_settings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    door: Callable[[], Coroutine[Any, Any, int]],
    first_store_await: str,
) -> None:
    async def _parked(self: InteractionStore, r: Any, *args: Any, **kwargs: Any) -> list[str]:
        await gate
        return []

    monkeypatch.setattr(InteractionStore, first_store_await, _parked)

    held = await _held_while_parked(door, gate, caplog)

    assert held == []
    assert "InteractionsSettings" not in caplog.text


@pytest.mark.usefixtures("real_accessor")
async def test_kill_park_suspended_on_the_kill_enqueue_holds_no_retired_settings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None]
) -> None:
    async def _read_kill_target(self: InteractionStore, r: Any, interaction_id: str) -> KillTarget:
        return KillTarget(kind="park", group_id="g-1", delivery=None, run_delivery_id="d-1", subjects=None)

    async def _enqueue_kill(self: InteractionStore, r: Any, *args: Any, **kwargs: Any) -> str:
        await gate
        return "gone"

    monkeypatch.setattr(InteractionStore, "read_kill_target", _read_kill_target)
    monkeypatch.setattr(InteractionStore, "enqueue_kill", _enqueue_kill)
    store = InteractionStore(interactions_settings().key_prefix)

    async def _door() -> str:
        return await kill_module.kill_park(object(), store, "i-1", "g-1", reason="expired")  # type: ignore[arg-type]

    held = await _held_while_parked(_door, gate, caplog)

    assert held == []
    assert "InteractionsSettings" not in caplog.text
