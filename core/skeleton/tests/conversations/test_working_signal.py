"""The per-turn working-on-it (typing) refresh loop: refresh, stop, ceiling and failure policy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Protocol

import pytest
from tai42_contract.channels import ChannelDeliveryError

from tai42_skeleton.app import instance as instance_module
from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations.models import ANSWERLESS_STATUSES, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import working_signal

from .conftest import durable_manager

_LOGGER = "tai42_skeleton.conversations.turn.working_signal"
_REAL_SLEEP = asyncio.sleep


def _record(
    message_id: str = "m1",
    *,
    door: str = "channel",
    channel: str | None = "line",
    status: DeliveryStatus = DeliveryStatus.ACCEPTED,
    provider_message_id: str | None = "wamid-1",
) -> ConversationRecord:
    now = time.time()
    answerless = status in ANSWERLESS_STATUSES
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door=door,  # type: ignore[arg-type]
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel=channel,
        our_identity="+15550001111",
        provider_message_id=provider_message_id,
        origin="client",
        inbound_text="ask",
        answer_status=None if answerless else "answered",
        answer=None if answerless else "hi",
        delivery_status=status,
        created_at=now,
        updated_at=now,
    )


class _CountingChannel:
    """Records each ``signal_working`` frame as ``(active, recipient, provider_message_id)``."""

    working_signal_expiry_seconds = 25.0

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[bool, str, str | None]] = []
        self._fail = fail

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))
        if self._fail:
            raise ChannelDeliveryError("vendor rejected the working signal")


class _FlakyChannel:
    """A channel whose ``active`` frames succeed or raise per a fixed schedule, so a test can
    interleave failures with successes; ``active=False`` clears never consume the schedule."""

    working_signal_expiry_seconds = 25.0

    def __init__(self, outcomes: list[bool]) -> None:
        self.calls: list[tuple[bool, str, str | None]] = []
        self._outcomes = iter(outcomes)

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))
        if active and not next(self._outcomes, True):
            raise ChannelDeliveryError("vendor rejected the working signal")


class _Channels:
    def __init__(self, channel, *, missing: bool = False) -> None:
        self._channel = channel
        self._missing = missing

    def get(self, name: str):
        if self._missing:
            raise KeyError(name)
        return self._channel


class _App:
    def __init__(self, channel, *, missing: bool = False) -> None:
        self.channels = _Channels(channel, missing=missing)


class _Clock:
    """A monotonic clock the fake sleep advances, so the ceiling is reached without real waits."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.t += max(delay, 0.0)
        await _REAL_SLEEP(0)


def _install_clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(working_signal, "_now", clock.now)
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    return clock


def _install_store(monkeypatch, reader) -> None:
    class _Store:
        async def get_record(self, message_id: str):
            return await reader(message_id)

    manager = durable_manager(records=_Store())
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)


class _CallRecorder(Protocol):
    calls: list[tuple[bool, str, str | None]]


def _actives(channel: _CallRecorder) -> list[tuple[bool, str, str | None]]:
    return [c for c in channel.calls if c[0] is True]


def _clears(channel: _CallRecorder) -> list[tuple[bool, str, str | None]]:
    return [c for c in channel.calls if c[0] is False]


async def test_refreshes_until_the_send_then_clears_once(monkeypatch):
    _install_clock(monkeypatch)
    channel = _CountingChannel()
    statuses = iter([DeliveryStatus.ACCEPTED, DeliveryStatus.PENDING_DELIVERY, DeliveryStatus.PROVISIONAL])

    async def _get_record(message_id: str) -> ConversationRecord:
        return _record(message_id, status=next(statuses))

    _install_store(monkeypatch, _get_record)
    record = _record()

    await working_signal._run_loop(record, channel, 25.0, ConversationsSettings())

    actives = _actives(channel)
    assert len(actives) >= 2
    assert len(_clears(channel)) == 1
    # The assert carries the record's participant address and inbound wamid.
    assert actives[0][1] == record.client_address
    assert actives[0][2] == record.provider_message_id


async def test_kill_switch_spawns_nothing(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS", "0")
    channel = _CountingChannel()
    monkeypatch.setattr(instance_module, "app", _App(channel))
    record = _record()

    working_signal.start(record)

    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS
    assert channel.calls == []


async def test_a_channel_with_no_expiry_never_loops(monkeypatch):
    class _NoExpiry:
        working_signal_expiry_seconds = None

        def __init__(self) -> None:
            self.calls: list = []

        async def signal_working(self, **kwargs) -> None:
            self.calls.append(kwargs)

    channel = _NoExpiry()
    monkeypatch.setattr(instance_module, "app", _App(channel))
    record = _record()

    working_signal.start(record)

    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS
    assert channel.calls == []


async def test_an_api_door_record_never_loops(monkeypatch):
    channel = _CountingChannel()
    monkeypatch.setattr(instance_module, "app", _App(channel))
    record = _record(door="api", channel=None, provider_message_id=None)

    working_signal.start(record)

    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS
    assert channel.calls == []


async def test_an_unregistered_channel_never_loops(monkeypatch, caplog):
    channel = _CountingChannel()
    monkeypatch.setattr(instance_module, "app", _App(channel, missing=True))
    record = _record()

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        working_signal.start(record)

    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS
    assert channel.calls == []
    assert any("not registered" in r.message for r in caplog.records)


async def test_three_consecutive_vendor_failures_stop_the_loop(monkeypatch, caplog):
    _install_clock(monkeypatch)
    channel = _CountingChannel(fail=True)

    async def _get_record(message_id: str) -> ConversationRecord:
        return _record(message_id, status=DeliveryStatus.ACCEPTED)

    _install_store(monkeypatch, _get_record)
    record = _record()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await working_signal._run_loop(record, channel, 25.0, ConversationsSettings())

    assert len(_actives(channel)) == 3
    assert _clears(channel) == []  # no assert ever succeeded, so nothing to clear
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3


async def test_the_ceiling_stops_a_loop_whose_status_never_leaves_in_flight(monkeypatch):
    _install_clock(monkeypatch)
    channel = _CountingChannel()

    async def _get_record(message_id: str) -> ConversationRecord:
        return _record(message_id, status=DeliveryStatus.PENDING_DELIVERY)

    _install_store(monkeypatch, _get_record)
    record = _record()
    settings = ConversationsSettings(working_signal_max_seconds=50.0, working_signal_refresh_margin_seconds=5.0)

    await working_signal._run_loop(record, channel, 25.0, settings)

    assert len(_actives(channel)) >= 1
    assert len(_clears(channel)) == 1


async def test_a_store_read_fault_stops_the_loop_with_a_warning(monkeypatch, caplog):
    _install_clock(monkeypatch)
    channel = _CountingChannel()

    async def _get_record(message_id: str) -> ConversationRecord:
        raise RuntimeError("store is unreachable")

    _install_store(monkeypatch, _get_record)
    record = _record()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await working_signal._run_loop(record, channel, 25.0, ConversationsSettings())

    assert len(_actives(channel)) == 1
    assert len(_clears(channel)) == 1
    assert any("stopping the signal" in r.message for r in caplog.records)


async def test_stop_cancels_a_running_loop_and_the_clear_fires(monkeypatch):
    channel = _CountingChannel()
    monkeypatch.setattr(instance_module, "app", _App(channel))
    reached_sleep = asyncio.Event()
    release = asyncio.Event()

    async def _parking_sleep(delay: float) -> None:
        reached_sleep.set()
        await release.wait()

    monkeypatch.setattr(asyncio, "sleep", _parking_sleep)
    record = _record()

    working_signal.start(record)
    task = working_signal._WORKING_SIGNAL_TASKS[record.message_id]
    await asyncio.wait_for(reached_sleep.wait(), timeout=1.0)

    working_signal.stop(record.message_id)
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert len(_actives(channel)) == 1
    assert len(_clears(channel)) == 1
    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS


async def test_a_missing_record_stops_the_loop(monkeypatch):
    _install_clock(monkeypatch)
    channel = _CountingChannel()

    async def _get_record(message_id: str) -> None:
        return None

    _install_store(monkeypatch, _get_record)
    record = _record()

    await working_signal._run_loop(record, channel, 25.0, ConversationsSettings())

    # One tick went out, then the record read back as gone (a cross-process send) and the loop stopped.
    assert len(_actives(channel)) == 1
    assert len(_clears(channel)) == 1


async def test_a_success_resets_the_consecutive_failure_counter(monkeypatch):
    # Failures come in pairs split by a success. Without the reset the third failure (the fourth
    # assert) would stop the loop; with it no run of failures reaches three, so every scheduled
    # assert runs and the loop stops only when the record leaves the in-flight set.
    _install_clock(monkeypatch)
    channel = _FlakyChannel([False, False, True, False, False, True, False, False])
    reads = {"n": 0}

    async def _get_record(message_id: str) -> ConversationRecord:
        reads["n"] += 1
        status = DeliveryStatus.ACCEPTED if reads["n"] < 8 else DeliveryStatus.PROVISIONAL
        return _record(message_id, status=status)

    _install_store(monkeypatch, _get_record)
    await working_signal._run_loop(_record(), channel, 25.0, ConversationsSettings())

    assert len(_actives(channel)) == 8  # a no-reset counter would have stopped after four
    assert len(_clears(channel)) == 1  # a success happened, so the loop cleared once


async def test_a_non_positive_expiry_warns_and_spawns_nothing(monkeypatch, caplog):
    class _ZeroExpiry:
        working_signal_expiry_seconds = 0.0

        def __init__(self) -> None:
            self.calls: list = []

        async def signal_working(self, **kwargs) -> None:
            self.calls.append(kwargs)

    channel = _ZeroExpiry()
    monkeypatch.setattr(instance_module, "app", _App(channel))
    record = _record()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        working_signal.start(record)

    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS
    assert channel.calls == []
    assert any("non-positive" in r.message and r.levelno == logging.WARNING for r in caplog.records)


async def test_a_second_start_for_the_same_id_cancels_the_first(monkeypatch):
    channel = _CountingChannel()
    monkeypatch.setattr(instance_module, "app", _App(channel))
    reached_sleep = asyncio.Event()
    release = asyncio.Event()

    async def _parking_sleep(delay: float) -> None:
        reached_sleep.set()
        await release.wait()

    monkeypatch.setattr(asyncio, "sleep", _parking_sleep)
    record = _record()

    working_signal.start(record)
    first = working_signal._WORKING_SIGNAL_TASKS[record.message_id]
    await asyncio.wait_for(reached_sleep.wait(), timeout=1.0)

    # A second start for the same message id cancels the running loop and installs the replacement.
    working_signal.start(record)
    second = working_signal._WORKING_SIGNAL_TASKS[record.message_id]
    assert second is not first

    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert first.cancelled()
    # The loser's teardown did not evict the successor the registry now holds.
    assert working_signal._WORKING_SIGNAL_TASKS[record.message_id] is second

    working_signal.stop(record.message_id)
    with contextlib.suppress(asyncio.CancelledError):
        await second


@pytest.mark.parametrize("door", ["intake", "api_door", "event_door"])
def test_every_scheduling_door_reaches_the_one_seam(door):
    # Every turn-scheduling door schedules through the single ``_schedule_turn`` seam, so the
    # loop starts once for all of them; the gate inside ``start`` handles the api door and opt-outs.
    from tai42_skeleton.conversations.turn.schedule import _schedule_turn

    door_module = __import__(f"tai42_skeleton.conversations.turn.{door}", fromlist=["_schedule_turn"])
    assert door_module._schedule_turn is _schedule_turn
