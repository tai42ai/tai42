"""The working-signal loop wired into the real turn and delivery seams.

Proves the ends of a turn's loop against the ACTUAL functions the doors run: the target seam
(``target._target_outcome``) starts the loop for the intake record once a target will run, so a
queued turn and a manual-mode thread show nothing; the turn's end stops it on every outcome, and
``_deliver_channel`` stops it the instant the answer is about to send.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from tai42_contract.template import TemplatedText

from tai42_skeleton.app import instance as instance_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_channel as delivery_channel_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.caps import TurnCaps
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.conversations.turn import schedule as schedule_module

from .conftest import (
    SETTLE_TIMEOUT_S,
    EchoAgent,
    FakeChannel,
    FakeManager,
    _channel_route,
    _settle,
    _store,
    _wire,
    create_record,
    fixed_settings,
)
from .test_manual_mode import ManualAgent
from .test_manual_mode import _channel_route as _moded_channel_route


class _SignalChannel:
    """A channel that advertises a vendor indicator and records each ``signal_working`` frame."""

    working_signal_expiry_seconds = 25.0

    def __init__(self) -> None:
        self.calls: list[tuple[bool, str, str | None]] = []
        self.asserted = asyncio.Event()
        self.cleared = asyncio.Event()

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))
        (self.asserted if active else self.cleared).set()


class _SignalChannels:
    def __init__(self, channel: _SignalChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> _SignalChannel:
        return self._channel


class _SignalApp:
    def __init__(self, channel: _SignalChannel) -> None:
        self.channels = _SignalChannels(channel)


def _answered_channel_record(message_id: str, answer: str) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id="PID-DELIVER",
        origin="client",
        inbound_text="ask",
        answer_status="answered",
        answer=answer,
        delivery_status=DeliveryStatus.PENDING_DELIVERY,
        created_at=now,
        updated_at=now,
    )


async def test_the_target_seam_starts_the_working_signal_for_the_intake_record(env, monkeypatch):
    # The single target seam every door's turn runs through must start the loop with the intake
    # record — proven against the REAL turn driven by a real accept.
    started: list[ConversationRecord] = []
    monkeypatch.setattr(working_signal, "start_turn", lambda record: started.append(record))

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    message_id = await turn_module.accept(
        "twilio",
        "+15550001111",
        "+15550002222",
        "+15550002222",
        "hi",
        "PID1",
        medium_message_id=None,
        reply_to_medium_message_id=None,
    )
    await _settle()

    assert [r.message_id for r in started] == [message_id]
    assert started[0].channel == "twilio"
    assert started[0].provider_message_id == "PID1"


class _AgentAwaitingTheIndicator(EchoAgent):
    """Holds its run until the channel shows the indicator, so the loop asserts while the target runs."""

    def __init__(self, channel: _SignalChannel) -> None:
        super().__init__()
        self._channel = channel

    async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
        await self._channel.asserted.wait()
        return await super().run(user_message=user_message, thread_id=thread_id, **kwargs)


async def _accept_hi() -> str:
    return await turn_module.accept(
        "twilio",
        "+15550001111",
        "+15550002222",
        "+15550002222",
        "hi",
        "PID1",
        medium_message_id=None,
        reply_to_medium_message_id=None,
    )


async def test_a_queued_turn_shows_no_indicator_until_it_is_admitted(env, monkeypatch):
    # One concurrency slot, held by another thread's turn: the accepted turn waits at the gate and
    # nothing is produced for its user, so no indicator shows until the gate admits it.
    monkeypatch.setenv("CONVERSATIONS_MAX_CONCURRENT_TURNS", "1")
    caps = TurnCaps(fixed_settings(ConversationsSettings()))
    monkeypatch.setattr(intake_module, "get_turn_caps", lambda: caps)
    signal_channel = _SignalChannel()
    monkeypatch.setattr(instance_module, "app", _SignalApp(signal_channel))
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    agent = _AgentAwaitingTheIndicator(signal_channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    holding = asyncio.Event()
    release = asyncio.Event()

    async def _other_threads_turn() -> None:
        caps.reserve_thread_slot("other-thread")
        async with caps.run_reserved("other-thread"):
            holding.set()
            await release.wait()

    other = asyncio.create_task(_other_threads_turn())
    try:
        await holding.wait()
        message_id = await _accept_hi()
        for _ in range(1000):
            if caps._gate._waiters:
                break
            await asyncio.sleep(0)
        assert len(caps._gate._waiters) == 1  # the turn stands at the gate

        assert signal_channel.calls == []
        assert not working_signal.has_loop(message_id)
    finally:
        release.set()
        await other
        await _settle()

    # Admitted, its target ran: the indicator showed, and the turn's end cleared it.
    await asyncio.wait_for(signal_channel.cleared.wait(), SETTLE_TIMEOUT_S)
    frames = [active for active, _, _ in signal_channel.calls]
    assert frames[0] is True
    assert frames[-1] is False
    assert all(frames[:-1])
    assert agent.calls == [("hi", "bridge:line:+15550002222")]


class _ManualAgentSeeingTheLoops(ManualAgent):
    """A memory-holding agent that records which working-signal loops run while the manual turn appends."""

    def __init__(self) -> None:
        super().__init__()
        self.loops_at_append: list[str] = []

    async def append_thread_messages(self, *, thread_id: str, messages, **kwargs) -> None:
        self.loops_at_append = list(working_signal._SIGNALS)
        await super().append_thread_messages(thread_id=thread_id, messages=messages, **kwargs)


async def test_a_manual_mode_thread_shows_no_indicator(env, monkeypatch):
    # A thread in manual mode runs no target and produces no reply: no loop runs and no frame is sent.
    signal_channel = _SignalChannel()
    monkeypatch.setattr(instance_module, "app", _SignalApp(signal_channel))
    _wire(monkeypatch, FakeManager(_moded_channel_route("manual")), FakeChannel())
    agent = _ManualAgentSeeingTheLoops()
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await _accept_hi()
    await _settle()

    assert agent.runs == []
    assert agent.appended == [("bridge:line:+15550002222", [{"role": "user", "content": "hi"}])]
    assert agent.loops_at_append == []
    assert not working_signal.has_loop(message_id)
    assert signal_channel.calls == []


async def test_deliver_channel_stops_the_records_working_signal_loop(env, monkeypatch):
    # A real loop is refreshing the record's indicator; the REAL _deliver_channel must cancel it
    # before the answer sends, so the loop stops and is evicted from the registry.
    async def _noop_grace(message_id: str, grace_seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_module, "_confirm_after_grace", _noop_grace)

    signal_channel = _SignalChannel()
    monkeypatch.setattr(instance_module, "app", _SignalApp(signal_channel))

    send_channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), send_channel)
    store = _store()
    record = _answered_channel_record("m-deliver", "hello")
    await create_record(store, record)

    working_signal.start_turn(record)
    task = working_signal._SIGNALS[record.message_id].task
    assert task is not None
    # Let the loop assert once and settle onto its (real, long) refresh sleep.
    for _ in range(200):
        if signal_channel.calls:
            break
        await asyncio.sleep(0)
    assert [c[0] for c in signal_channel.calls] == [True]

    stored = await store.get_record(record.message_id)
    assert stored is not None
    await delivery_channel_module._deliver_channel(store, stored, "worker-1")

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert not working_signal.has_loop(record.message_id)
    # The answer really went out through the delivery path, and the loop cleared its indicator once.
    assert [n.message for n in send_channel.sends] == ["hello"]
    assert signal_channel.calls[-1][0] is False


def _finished_turn(outcome: str) -> asyncio.Future:
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    if outcome == "cancelled":
        future.cancel()
    elif outcome == "failed":
        future.set_exception(RuntimeError("the turn crashed"))
    else:
        status = DeliveryStatus.SILENT if outcome == "silent" else DeliveryStatus.PENDING_DELIVERY
        record = _answered_channel_record("m-turn", "hello").model_copy(update={"delivery_status": status})
        future.set_result(record)
    return future


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "silent", "answered"])
async def test_the_turns_end_stops_its_working_signal_on_every_outcome(outcome, monkeypatch):
    stopped: list[str] = []
    monkeypatch.setattr(working_signal, "stop", stopped.append)
    monkeypatch.setattr(schedule_module, "spawn_delivery", lambda message_id: None)
    monkeypatch.setattr(schedule_module, "_spawn_intake_resolution", lambda message_id: None)

    schedule_module._spawn_delivery_on_success(_finished_turn(outcome), "m-turn")  # type: ignore[arg-type]

    assert stopped == ["m-turn"]
