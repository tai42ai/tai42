"""The working-signal loop wired into the real turn and delivery seams.

Proves the two ends of the loop against the ACTUAL functions the doors run: ``_schedule_turn``
starts the loop for the intake record, and ``_deliver_channel`` stops it the instant the answer
is about to send.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from tai42_skeleton.app import instance as instance_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_channel as delivery_channel_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import working_signal

from .conftest import (
    EchoAgent,
    FakeChannel,
    FakeManager,
    _channel_route,
    _settle,
    _store,
    _wire,
)


class _SignalChannel:
    """A channel that advertises a vendor indicator and records each ``signal_working`` frame."""

    working_signal_expiry_seconds = 25.0

    def __init__(self) -> None:
        self.calls: list[tuple[bool, str, str | None]] = []

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))


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


async def test_schedule_turn_starts_the_working_signal_for_the_intake_record(env, monkeypatch):
    # The single scheduling seam every door funnels through must start the loop with the intake
    # record — proven against the REAL _schedule_turn driven by a real accept.
    started: list[ConversationRecord] = []
    monkeypatch.setattr(working_signal, "start", lambda record: started.append(record))

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert [r.message_id for r in started] == [message_id]
    assert started[0].channel == "twilio"
    assert started[0].provider_message_id == "PID1"


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
    await store.create_record(record)

    working_signal.start(record)
    task = working_signal._WORKING_SIGNAL_TASKS[record.message_id]
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
    assert record.message_id not in working_signal._WORKING_SIGNAL_TASKS
    # The answer really went out through the delivery path, and the loop cleared its indicator once.
    assert [n.message for n in send_channel.sends] == ["hello"]
    assert signal_channel.calls[-1][0] is False
