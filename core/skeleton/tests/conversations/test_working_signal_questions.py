"""The working-signal loop pauses while a question the run waits on stands and shows the indicator again after."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.models import DeliveryStatus

from ._working_signal_support import (
    OTHER_ADDRESS,
    TARGET,
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
    # A 25 s lifetime and a 5 s margin refresh every 20 s, so a 60 s stretch fits 3 asserts.
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


@contextlib.contextmanager
def _held():
    with working_signal.hold(RUN, thread_id=THREAD):
        yield


async def test_a_fresh_run_loop_shows_nothing_until_no_question_it_sent_stands(env):
    clock, channel, store = env
    store.ask_on("x")
    store.ask_on("y")
    with _held():
        await clock.advance(40.0)
        assert channel.calls == []

        working_signal.resumed(RUN, "x", medium_message_id="MX", pending=["y"])
        await clock.advance(40.0)
        assert channel.calls == []

        working_signal.resumed(RUN, "y", medium_message_id="MY", pending=[])
        await settle()
        assert actives(channel) == [(True, TARGET[2], "MY")]


async def test_a_turn_loop_pauses_for_a_standing_sibling_and_keeps_the_intake_id(env):
    clock, channel, store = env
    store.ask_on("y")
    rec = record("turn-1")
    working_signal.start_turn(rec)
    await settle()
    assert actives(channel) == [(True, TARGET[2], "wamid-1")]

    working_signal.resumed(rec.message_id, "x", medium_message_id=None, pending=["y"])
    await clock.advance(40.0)
    assert len(clears(channel)) == 1
    assert len(actives(channel)) == 1

    working_signal.resumed(rec.message_id, "y", medium_message_id=None, pending=[])
    await settle()
    assert actives(channel)[-1] == (True, TARGET[2], "wamid-1")
    assert len(actives(channel)) == 2


async def test_a_sibling_asked_of_another_user_keeps_the_loop_paused_until_it_is_answered(env):
    """The run waits on the other person's answer, so its own user is shown nothing meanwhile."""
    clock, channel, store = env
    store.ask_on("x")
    store.ask_on("z", client_address=OTHER_ADDRESS)
    with _held():
        working_signal.resumed(RUN, "x", medium_message_id="MX", pending=["z"])
        await clock.advance(40.0)
        assert channel.calls == []

        # The other person's message id is never lent; the user's own answer's id still is.
        working_signal.resumed(RUN, "z", medium_message_id="MZ", pending=[])
        await settle()
        assert actives(channel) == [(True, TARGET[2], "MX")]


async def test_the_ceiling_ends_a_stretch_never_the_loop(env):
    clock, channel, _ = env
    with _held():
        working_signal.resumed(RUN, "x", medium_message_id=None, pending=[])
        await clock.advance(200.0)
        assert len(actives(channel)) == 3
        assert len(clears(channel)) == 1
        assert working_signal.has_loop(RUN)

        working_signal.resumed(RUN, "y", medium_message_id=None, pending=[])
        await clock.advance(200.0)
        assert len(actives(channel)) == 6
        assert len(clears(channel)) == 2
    await settle()
    assert not working_signal.has_loop(RUN)
    assert len(clears(channel)) == 2


async def test_a_resume_mid_stretch_does_not_restart_the_ceiling(env):
    clock, channel, _ = env
    with _held():
        working_signal.resumed(RUN, "x", medium_message_id=None, pending=[])
        await clock.advance(30.0)
        working_signal.resumed(RUN, "x", medium_message_id=None, pending=[])
        await clock.advance(200.0)
        assert len(actives(channel)) == 3
        assert len(clears(channel)) == 1


async def test_a_question_to_the_user_pauses_and_its_answer_resumes(env):
    clock, channel, _ = env
    rec = record("turn-1")
    working_signal.start_turn(rec)
    await settle()

    working_signal.question_sent(rec.message_id, "q")
    await clock.advance(40.0)
    assert len(clears(channel)) == 1
    assert len(actives(channel)) == 1

    working_signal.question_answered(rec.message_id, "q", None)
    await settle()
    assert len(actives(channel)) == 2


async def test_a_withdrawn_question_resumes_with_the_same_inbound_id(env):
    clock, channel, _ = env
    rec = record("turn-1")
    working_signal.start_turn(rec)
    await settle()

    working_signal.question_sent(rec.message_id, "q")
    await clock.advance(40.0)
    working_signal.question_withdrawn(rec.message_id, "q")
    await settle()

    assert len(clears(channel)) == 1
    assert actives(channel)[-1] == (True, TARGET[2], "wamid-1")
    # The resume opened a fresh stretch: three asserts fit it, then one clear.
    await clock.advance(200.0)
    assert len(actives(channel)) == 4
    assert len(clears(channel)) == 2


async def test_withdrawing_an_unknown_question_changes_nothing(env):
    clock, channel, _ = env
    working_signal.start_turn(record("turn-1"))
    await clock.advance(30.0)
    working_signal.question_withdrawn("turn-1", "never-asked")
    await clock.advance(200.0)

    assert len(actives(channel)) == 3
    assert len(clears(channel)) == 1


async def test_a_question_to_another_user_pauses_the_loop_until_its_answer(env):
    """The run waits on another person's answer, so its own user's indicator is cleared and not refreshed."""
    clock, channel, store = env
    store.ask_on("q", client_address=OTHER_ADDRESS)
    rec = record("turn-1")
    working_signal.start_turn(rec)
    await settle()
    assert len(actives(channel)) == 1

    working_signal.question_sent(rec.message_id, "q")
    await clock.advance(50.0)  # inside the 60 s stretch; the other person has not answered yet
    assert len(clears(channel)) == 1
    assert len(actives(channel)) == 1

    # The answer shows the indicator again; the other person's message id is never lent to it.
    working_signal.question_answered(rec.message_id, "q", "M-OTHER")
    await settle()
    assert actives(channel)[-1] == (True, TARGET[2], "wamid-1")
    assert len(actives(channel)) == 2


async def test_an_answer_after_the_record_left_flight_shows_nothing_and_ends_the_loop(env):
    _, channel, store = env
    rec = record("turn-1")
    working_signal.start_turn(rec)
    await settle()
    working_signal.question_sent(rec.message_id, "q")
    await settle()
    assert len(actives(channel)) == 1
    assert len(clears(channel)) == 1

    store.records[rec.message_id] = record(rec.message_id, status=DeliveryStatus.DELIVERED)
    working_signal.question_answered(rec.message_id, "q", None)
    await settle()

    assert not working_signal.has_loop(rec.message_id)
    assert len(actives(channel)) == 1
    assert len(clears(channel)) == 1


async def test_an_answer_lends_its_message_id_to_the_next_assert(env):
    _, channel, store = env
    store.ask_on("q")
    with _held():
        working_signal.resumed(RUN, "x", medium_message_id=None, pending=[])
        await settle()
        assert actives(channel) == [(True, TARGET[2], None)]

        working_signal.question_sent(RUN, "q")
        await settle()
        working_signal.question_answered(RUN, "q", "M")
        await settle()
        assert actives(channel)[-1] == (True, TARGET[2], "M")


async def test_calls_before_the_user_is_known_are_applied_in_order(env, monkeypatch):
    _, channel, _ = env
    gate = asyncio.Event()
    install_run_target(monkeypatch, gate=gate)
    with _held():
        await settle()
        working_signal.resumed(RUN, "x", medium_message_id=None, pending=[])
        working_signal.question_sent(RUN, "q")
        gate.set()
        await settle()
        assert actives(channel) == []


async def test_a_question_withdrawn_before_its_loop_knows_its_user_leaves_the_loop_showing(env, monkeypatch):
    _, channel, _ = env
    gate = asyncio.Event()
    install_run_target(monkeypatch, gate=gate)
    with _held():
        await settle()
        working_signal.resumed(RUN, "x", medium_message_id=None, pending=[])
        working_signal.question_sent(RUN, "q")
        working_signal.question_withdrawn(RUN, "q")
        gate.set()
        await settle()
        assert len(actives(channel)) == 1
