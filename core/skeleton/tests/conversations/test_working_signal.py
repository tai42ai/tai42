"""The working-on-it (typing) refresh loop of a turn: refresh grid, stretch ceiling, stop and failures."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math

import pytest
from tai42_contract.channels import ChannelDeliveryError

from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus

from ._working_signal_support import (
    LOGGER,
    REAL_SLEEP,
    CountingChannel,
    GatedChannel,
    InstantClock,
    ManualClock,
    SendStore,
    actives,
    clears,
    install_app,
    install_clock,
    install_store,
    record,
    stop_all,
)


@pytest.fixture(autouse=True)
async def _no_loop_left():
    yield
    await stop_all()


def _limits(monkeypatch, *, ceiling: float, margin: float = 5.0) -> None:
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS", str(ceiling))
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_REFRESH_MARGIN_SECONDS", str(margin))


class _RecordReader:
    """A store whose run record reads come from ``reader``."""

    def __init__(self, reader) -> None:
        self._reader = reader

    async def get_record(self, message_id: str):
        return await self._reader(message_id)

    async def ask_send_message_id(self, interaction_id: str) -> str | None:
        return None


def _install_reader(monkeypatch, reader) -> None:
    install_store(monkeypatch, _RecordReader(reader))


async def _run_turn(rec: ConversationRecord, *, until=lambda: False, steps: int = 10_000) -> asyncio.Task:
    """Start ``rec``'s turn loop and let it run until ``until()`` holds or it ends; then stop it."""
    working_signal.start_turn(rec)
    task = working_signal._SIGNALS[rec.message_id].task
    assert task is not None
    for _ in range(steps):
        if until() or task.done():
            break
        await REAL_SLEEP(0)
    else:
        pytest.fail("the loop neither met the condition nor ended")
    working_signal.stop(rec.message_id)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


async def test_refreshes_until_the_send_then_clears_once(monkeypatch):
    install_clock(monkeypatch, InstantClock())
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    statuses = iter([DeliveryStatus.ACCEPTED, DeliveryStatus.PENDING_DELIVERY, DeliveryStatus.PROVISIONAL])

    async def _get_record(message_id: str) -> ConversationRecord:
        return record(message_id, status=next(statuses))

    _install_reader(monkeypatch, _get_record)
    rec = record()

    await _run_turn(rec)

    assert len(actives(channel)) >= 2
    assert len(clears(channel)) == 1
    # The assert carries the record's user address and inbound message id.
    assert actives(channel)[0][1] == rec.client_address
    assert actives(channel)[0][2] == rec.provider_message_id
    assert rec.message_id not in working_signal._SIGNALS


async def test_kill_switch_starts_nothing(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS", "0")
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    rec = record()

    working_signal.start_turn(rec)

    assert not working_signal.has_loop(rec.message_id)
    assert channel.calls == []


async def test_a_channel_with_no_expiry_never_loops(monkeypatch):
    class _NoExpiry:
        working_signal_expiry_seconds = None

        def __init__(self) -> None:
            self.calls: list = []

        async def signal_working(self, **kwargs) -> None:
            self.calls.append(kwargs)

    channel = _NoExpiry()
    install_app(monkeypatch, channel)
    rec = record()

    working_signal.start_turn(rec)

    assert not working_signal.has_loop(rec.message_id)
    assert channel.calls == []


async def test_an_api_door_record_never_loops(monkeypatch):
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    rec = record(door="api", channel=None, provider_message_id=None)

    working_signal.start_turn(rec)

    assert not working_signal.has_loop(rec.message_id)
    assert channel.calls == []


async def test_an_unregistered_channel_never_loops(monkeypatch, caplog):
    channel = CountingChannel()
    install_app(monkeypatch, channel, missing=True)
    rec = record()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        working_signal.start_turn(rec)

    assert not working_signal.has_loop(rec.message_id)
    assert channel.calls == []
    assert any("not registered" in r.message for r in caplog.records)


async def test_a_non_positive_expiry_warns_and_starts_nothing(monkeypatch, caplog):
    class _ZeroExpiry:
        working_signal_expiry_seconds = 0.0

        def __init__(self) -> None:
            self.calls: list = []

        async def signal_working(self, **kwargs) -> None:
            self.calls.append(kwargs)

    channel = _ZeroExpiry()
    install_app(monkeypatch, channel)
    rec = record()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        working_signal.start_turn(rec)

    assert not working_signal.has_loop(rec.message_id)
    assert channel.calls == []
    assert any("non-positive" in r.message and r.levelno == logging.WARNING for r in caplog.records)


async def test_three_consecutive_vendor_failures_end_the_loop(monkeypatch, caplog):
    install_clock(monkeypatch, InstantClock())
    channel = CountingChannel(fail=True)
    install_app(monkeypatch, channel)

    async def _get_record(message_id: str) -> ConversationRecord:
        return record(message_id, status=DeliveryStatus.ACCEPTED)

    _install_reader(monkeypatch, _get_record)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = await _run_turn(record())

    assert not task.cancelled()
    assert len(actives(channel)) == 3
    assert clears(channel) == []  # no assert ever succeeded, so nothing to clear
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3


async def test_a_success_resets_the_consecutive_failure_counter(monkeypatch):
    # Failures come in pairs split by a success. Without the reset the third failure (the fourth
    # assert) would end the loop; with it no run of failures reaches three, so every scheduled
    # assert runs and the loop ends only when the record leaves the in-flight set. The record is
    # read once before each assert: the ninth read finds it out of flight.
    install_clock(monkeypatch, InstantClock())
    channel = _FlakyChannel([False, False, True, False, False, True, False, False])
    install_app(monkeypatch, channel)
    reads = {"n": 0}

    async def _get_record(message_id: str) -> ConversationRecord:
        reads["n"] += 1
        status = DeliveryStatus.ACCEPTED if reads["n"] < 9 else DeliveryStatus.PROVISIONAL
        return record(message_id, status=status)

    _install_reader(monkeypatch, _get_record)
    await _run_turn(record())

    assert len(actives(channel)) == 8  # a no-reset counter would have ended after four
    assert len(clears(channel)) == 1  # a success happened, so the loop cleared once


class _FlakyChannel(CountingChannel):
    """A channel whose ``active`` frames succeed or raise per a fixed schedule; clears never consume it."""

    def __init__(self, outcomes: list[bool]) -> None:
        super().__init__()
        self._outcomes = iter(outcomes)

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))
        if active and not next(self._outcomes, True):
            raise ChannelDeliveryError("vendor rejected the working signal")


async def test_a_store_read_fault_ends_the_loop_with_a_warning(monkeypatch, caplog):
    install_clock(monkeypatch, InstantClock())
    channel = CountingChannel()
    install_app(monkeypatch, channel)

    async def _get_record(message_id: str) -> ConversationRecord:
        raise RuntimeError("store is unreachable")

    _install_reader(monkeypatch, _get_record)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run_turn(record())

    assert channel.calls == []
    assert any("stopping the signal" in r.message for r in caplog.records)


async def test_a_record_out_of_flight_ends_the_loop_and_a_missing_record_does_not(monkeypatch):
    clock = install_clock(monkeypatch, ManualClock())
    _limits(monkeypatch, ceiling=1000.0)
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    store = SendStore()
    install_store(monkeypatch, store)
    rec = record()

    working_signal.start_turn(rec)
    await clock.advance(60.0)  # three refresh ticks, each reading a record that is not there
    assert len(actives(channel)) == 4
    assert working_signal.has_loop(rec.message_id)

    store.records[rec.message_id] = record(rec.message_id, status=DeliveryStatus.PROVISIONAL)
    await clock.advance(20.0)

    assert not working_signal.has_loop(rec.message_id)
    assert len(actives(channel)) == 4
    assert len(clears(channel)) == 1


async def test_a_turn_whose_record_left_flight_before_its_first_assert_shows_nothing(monkeypatch):
    clock = install_clock(monkeypatch, ManualClock())
    _limits(monkeypatch, ceiling=1000.0)
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    store = SendStore()
    install_store(monkeypatch, store)
    rec = record()
    store.records[rec.message_id] = record(rec.message_id, status=DeliveryStatus.DELIVERED)

    working_signal.start_turn(rec)
    await clock.advance(60.0)

    assert store.record_reads == [rec.message_id]
    assert not working_signal.has_loop(rec.message_id)
    assert channel.calls == []


async def test_stop_cancels_a_running_loop_and_the_clear_fires(monkeypatch):
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    install_store(monkeypatch, SendStore())
    reached_sleep = asyncio.Event()
    release = asyncio.Event()

    async def _parking_sleep(delay: float) -> None:
        reached_sleep.set()
        await release.wait()

    monkeypatch.setattr(asyncio, "sleep", _parking_sleep)
    rec = record()

    working_signal.start_turn(rec)
    task = working_signal._SIGNALS[rec.message_id].task
    assert task is not None
    await asyncio.wait_for(reached_sleep.wait(), timeout=1.0)

    working_signal.stop(rec.message_id)
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert len(actives(channel)) == 1
    assert len(clears(channel)) == 1
    assert not working_signal.has_loop(rec.message_id)


async def test_a_stop_during_the_first_assert_still_clears_once(monkeypatch):
    install_clock(monkeypatch, ManualClock())
    channel = GatedChannel()
    install_app(monkeypatch, channel)
    install_store(monkeypatch, SendStore())
    rec = record()

    working_signal.start_turn(rec)
    task = working_signal._SIGNALS[rec.message_id].task
    assert task is not None
    await asyncio.wait_for(channel.entered.wait(), timeout=1.0)

    working_signal.stop(rec.message_id)
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert len(actives(channel)) == 1
    assert len(clears(channel)) == 1
    assert channel.finished == [False]


# -- the stretch ceiling and the refresh grid ----------------------------------------------------


async def test_the_ceiling_clears_once_and_leaves_the_loop_dormant(monkeypatch):
    clock = install_clock(monkeypatch, ManualClock())
    _limits(monkeypatch, ceiling=60.0)
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    install_store(monkeypatch, SendStore())
    rec = record()

    working_signal.start_turn(rec)
    await clock.advance(200.0)

    # A 20 s interval (25 s lifetime, 5 s margin) fits ceil(60 / 20) = 3 asserts in the stretch.
    assert len(actives(channel)) == math.ceil(60.0 / 20.0)
    assert len(clears(channel)) == 1
    assert working_signal.has_loop(rec.message_id)

    working_signal.stop(rec.message_id)
    await REAL_SLEEP(0)
    assert len(clears(channel)) == 1  # nothing was shown since the ceiling, so the stop clears nothing


class _SlowCountingChannel(CountingChannel):
    """A counting channel whose every frame takes ``latency`` seconds of the fake clock and notes the
    instant each frame began."""

    def __init__(self, clock: InstantClock, *, latency: float) -> None:
        super().__init__()
        self._clock = clock
        self._latency = latency
        self.started_at: list[float] = []

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.started_at.append(self._clock.t)
        await super().signal_working(
            recipient=recipient, sender_identity=sender_identity, provider_message_id=provider_message_id, active=active
        )
        self._clock.t += self._latency


class _MillisecondClock(InstantClock):
    """An event loop whose timers fire on a whole-millisecond grid (uvloop rounds every delay to the
    nearest millisecond), so ``asyncio.sleep(d)`` can end up to half a millisecond before ``d`` has
    passed on the monotonic clock the stretch deadline reads."""

    async def sleep(self, delay: float) -> None:
        self.t += max(round(delay * 1000) / 1000, 0.0)
        await REAL_SLEEP(0)


async def _run_stretch(monkeypatch, clock: InstantClock, channel: CountingChannel, *, ceiling: float) -> None:
    """Run one turn's first stretch on ``clock`` (2.0 s lifetime, so a 1.0 s interval) to its clear."""
    install_clock(monkeypatch, clock)
    install_app(monkeypatch, channel)
    _limits(monkeypatch, ceiling=ceiling)

    async def _get_record(message_id: str) -> ConversationRecord:
        return record(message_id, status=DeliveryStatus.PENDING_DELIVERY)

    _install_reader(monkeypatch, _get_record)
    monkeypatch.setattr(channel, "working_signal_expiry_seconds", 2.0)
    await _run_turn(record(), until=lambda: bool(clears(channel)))


async def test_a_millisecond_timer_grid_adds_no_refresh_at_the_ceiling(monkeypatch):
    clock = _MillisecondClock()
    # Each frame takes 33.4 ms, so the remainder left before the ceiling after the second frame
    # (1.2 - 2 * 0.0334 - 1.0 = 0.1332 s) rounds DOWN onto the millisecond grid and the remainder
    # sleep ends 0.2 ms short of the ceiling.
    channel = _SlowCountingChannel(clock, latency=0.0334)

    # A 1.0 s interval and a 1.2 s ceiling fit the first assert and one refresh: ceil(1.2 / 1.0) = 2.
    await _run_stretch(monkeypatch, clock, channel, ceiling=1.2)

    assert len(actives(channel)) == 2, channel.started_at
    assert len(clears(channel)) == 1


class _EarlyClock(InstantClock):
    """An event loop whose every timer fires 0.4 ms before its delay has passed on the monotonic
    clock — the most a whole-millisecond timer grid can cut off a sleep."""

    async def sleep(self, delay: float) -> None:
        self.t += max(delay - 0.0004, 0.0)
        await REAL_SLEEP(0)


async def test_early_timers_add_no_refresh_when_the_ceiling_is_a_whole_number_of_intervals(monkeypatch):
    clock = _EarlyClock()
    channel = _SlowCountingChannel(clock, latency=0.0)

    # Refreshes are due at 0.0 and 1.0 s; the one due at 2.0 s is AT the ceiling, so
    # ceil(2.0 / 1.0) = 2 asserts however early each timer fires.
    await _run_stretch(monkeypatch, clock, channel, ceiling=2.0)

    assert len(actives(channel)) == 2, channel.started_at
    assert len(clears(channel)) == 1
    assert all(started < 2.0 for started in channel.started_at[:2])


class _StallingChannel(_SlowCountingChannel):
    """A counting channel whose FIRST frame stalls for ``stall`` seconds of the fake clock; every
    later frame is instant."""

    def __init__(self, clock: InstantClock, *, stall: float) -> None:
        super().__init__(clock, latency=0.0)
        self._stall = stall

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        await super().signal_working(
            recipient=recipient, sender_identity=sender_identity, provider_message_id=provider_message_id, active=active
        )
        if len(self.started_at) == 1:
            self._clock.t += self._stall


async def test_a_stalled_frame_skips_the_refreshes_it_overran_instead_of_bursting(monkeypatch):
    clock = InstantClock()
    # The first frame takes 2.5 intervals, overrunning the refreshes due at 1.0 and 2.0 s.
    channel = _StallingChannel(clock, stall=2.5)

    await _run_stretch(monkeypatch, clock, channel, ceiling=6.0)

    # The overrun slots are skipped, never sent back to back: the next refresh is the 3.0 s slot,
    # then one per 1.0 s interval up to the last slot before the 6.0 s ceiling.
    assert channel.started_at[:-1] == [0.0, 3.0, 4.0, 5.0]
    assert len(clears(channel)) == 1


class _OverrunningClock(InstantClock):
    """A starved event loop: every sleep ends ``overrun`` seconds after its delay."""

    def __init__(self, overrun: float) -> None:
        super().__init__()
        self._overrun = overrun

    async def sleep(self, delay: float) -> None:
        self.t += max(delay, 0.0) + self._overrun
        await REAL_SLEEP(0)


async def test_a_sleep_that_overruns_the_ceiling_sends_nothing_past_it(monkeypatch):
    clock = _OverrunningClock(overrun=5.0)
    channel = _SlowCountingChannel(clock, latency=0.0)

    # The refresh due at 1.0 s is woken at 6.0 s, past the 3.0 s ceiling: the stretch closes there.
    await _run_stretch(monkeypatch, clock, channel, ceiling=3.0)

    assert len(actives(channel)) == 1
    assert len(clears(channel)) == 1


@pytest.mark.parametrize("door", ["intake", "api_door", "event_door"])
def test_every_scheduling_door_reaches_the_one_seam(door):
    # Every turn-scheduling door schedules through the single ``_schedule_turn`` seam, whose turn
    # runs its target through ``target._target_outcome``, so the loop starts at that one place for all
    # of them; the gate inside ``start_turn`` handles the api door and opt-outs.
    from tai42_skeleton.conversations.turn.schedule import _schedule_turn

    door_module = __import__(f"tai42_skeleton.conversations.turn.{door}", fromlist=["_schedule_turn"])
    assert door_module._schedule_turn is _schedule_turn
