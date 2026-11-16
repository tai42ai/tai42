"""A run's working-signal loop lives while drives hold it: holds, releases, stop and the loop's own end."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.settings import conversations_settings

from ._working_signal_support import (
    LOGGER,
    CountingChannel,
    ManualClock,
    SendStore,
    actives,
    clears,
    install_app,
    install_clock,
    install_run_target,
    install_store,
    record,
    settle,
    stop_all,
)

RUN = "run-1"
THREAD = "bridge:line:+15550002222"


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS", "60")
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_REFRESH_MARGIN_SECONDS", "5")
    clock = install_clock(monkeypatch, ManualClock())
    channel = CountingChannel()
    install_app(monkeypatch, channel)
    store = SendStore()
    install_store(monkeypatch, store)
    install_run_target(monkeypatch)
    yield clock, channel, store
    await stop_all()


def _enter(run_id: str = RUN) -> contextlib.AbstractContextManager[None]:
    held = working_signal.hold(run_id, thread_id=THREAD)
    held.__enter__()
    return held


def _exit(held: contextlib.AbstractContextManager[None]) -> None:
    held.__exit__(None, None, None)


async def _assert_from_now(run_id: str = RUN) -> None:
    """Empty the run's standing set so a stretch opens and the loop asserts at once."""
    working_signal.resumed(run_id, "answered", medium_message_id=None, pending=[])
    await settle()


async def test_two_holds_share_one_loop_and_the_last_release_ends_it(env):
    clock, channel, _ = env
    first = _enter()
    signal = working_signal._SIGNALS[RUN]
    second = _enter()
    assert working_signal._SIGNALS[RUN] is signal
    await _assert_from_now()
    assert len(actives(channel)) == 1

    _exit(first)
    await clock.advance(20.0)
    assert working_signal.has_loop(RUN)
    assert len(actives(channel)) == 2

    _exit(second)
    await settle()
    assert not working_signal.has_loop(RUN)
    assert signal.task is not None
    assert signal.task.done()
    assert len(clears(channel)) == 1


async def test_a_release_never_ends_a_loop_that_replaced_its_own(env):
    _, channel, _ = env
    first = _enter()
    working_signal.stop(RUN)
    await settle()
    second = _enter()
    replacement = working_signal._SIGNALS[RUN]

    _exit(first)
    await settle()

    assert working_signal._SIGNALS[RUN] is replacement
    assert replacement.holds == 1
    assert replacement.task is not None
    assert not replacement.task.done()
    _exit(second)
    await settle()
    assert not working_signal.has_loop(RUN)
    assert channel.calls == []


async def test_overlapping_holds_end_with_the_last_holder_whichever_started_the_loop(env):
    clock, channel, _ = env
    creator = _enter()
    joiner = _enter()
    await _assert_from_now()

    _exit(creator)
    await clock.advance(20.0)
    assert len(actives(channel)) == 2
    assert clears(channel) == []

    _exit(joiner)
    await settle()
    assert not working_signal.has_loop(RUN)
    assert len(clears(channel)) == 1


async def test_a_release_with_nothing_shown_clears_nothing(env):
    clock, channel, _ = env
    held = _enter()
    await clock.advance(60.0)
    _exit(held)
    await settle()
    assert not working_signal.has_loop(RUN)
    assert channel.calls == []


async def test_the_kill_switch_holds_nothing(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS", "0")
    conversations_settings.cache_clear()
    with working_signal.hold(RUN, thread_id=THREAD):
        assert not working_signal.has_loop(RUN)


async def test_a_target_resolution_failure_logs_one_error_and_evicts_the_loop(env, monkeypatch, caplog):
    _, channel, _ = env
    install_run_target(monkeypatch, fail=True)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        held = _enter()
        await settle()

    assert not working_signal.has_loop(RUN)
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    # Every later call for the run finds no loop and does nothing.
    _exit(held)
    working_signal.resumed(RUN, "q", medium_message_id="M", pending=[])
    working_signal.stop(RUN)
    await settle()
    assert channel.calls == []


async def test_a_send_record_read_fault_logs_one_error_and_evicts_the_loop(env, caplog):
    _, channel, store = env
    held = _enter()
    await _assert_from_now()
    store.fail_ask_reads = True

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        # An answer carrying its message id reads its ask's send record to tell whose it is.
        working_signal.resumed(RUN, "q", medium_message_id="M", pending=[])
        await settle()

    assert not working_signal.has_loop(RUN)
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    assert len(clears(channel)) == 1
    _exit(held)
    working_signal.stop(RUN)
    await settle()
    assert len(clears(channel)) == 1


async def test_the_runs_record_out_of_flight_ends_the_loop_and_a_missing_one_does_not(env):
    clock, channel, store = env
    held = _enter()
    await _assert_from_now()
    await clock.advance(20.0)  # the tick reads the run's record: not there yet, so the loop goes on
    assert RUN in store.record_reads
    assert len(actives(channel)) == 2

    store.records[RUN] = record(RUN, status=DeliveryStatus.PROVISIONAL)
    await clock.advance(20.0)

    assert not working_signal.has_loop(RUN)
    assert len(actives(channel)) == 2
    assert len(clears(channel)) == 1
    _exit(held)


async def test_a_held_run_whose_record_left_flight_shows_nothing_and_its_loop_ends(env):
    _, channel, store = env
    store.records[RUN] = record(RUN, status=DeliveryStatus.DELIVERED)
    held = _enter()
    await _assert_from_now()

    assert not working_signal.has_loop(RUN)
    assert RUN not in working_signal._SIGNALS
    assert channel.calls == []
    _exit(held)
    await settle()
    assert channel.calls == []


async def test_stop_ends_a_held_loop_and_clears_once(env):
    _, channel, _ = env
    held = _enter()
    signal = working_signal._SIGNALS[RUN]
    await _assert_from_now()

    working_signal.stop(RUN)
    assert signal.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await signal.task

    assert len(clears(channel)) == 1
    _exit(held)
    await settle()
    assert len(clears(channel)) == 1
