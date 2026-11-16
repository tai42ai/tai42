"""Fakes the working-signal tests share: the recording channels, the clocks, a conversations store."""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from typing import Protocol

from tai42_contract.channels import ChannelDeliveryError

from tai42_skeleton.app import instance as instance_module
from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.models import ANSWERLESS_STATUSES, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.turn import completion_delivery
from tai42_skeleton.conversations.turn.errors import CompletionDeliveryError

from .conftest import durable_manager

LOGGER = "tai42_skeleton.conversations.working_signal"
REAL_SLEEP = asyncio.sleep

CHANNEL = "line"
OUR_IDENTITY = "+15550001111"
USER_ADDRESS = "+15550002222"
OTHER_ADDRESS = "+15550003333"
TARGET = (CHANNEL, OUR_IDENTITY, USER_ADDRESS)


def record(
    message_id: str = "m1",
    *,
    door: str = "channel",
    channel: str | None = CHANNEL,
    status: DeliveryStatus = DeliveryStatus.ACCEPTED,
    provider_message_id: str | None = "wamid-1",
    client_address: str = USER_ADDRESS,
) -> ConversationRecord:
    now = time.time()
    answerless = status in ANSWERLESS_STATUSES
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door=door,  # type: ignore[arg-type]
        thread_id=f"bridge:line:{client_address}",
        client_address=client_address,
        channel=channel,
        our_identity=OUR_IDENTITY,
        provider_message_id=provider_message_id,
        origin="client",
        inbound_text="ask",
        answer_status=None if answerless else "answered",
        answer=None if answerless else "hi",
        delivery_status=status,
        created_at=now,
        updated_at=now,
    )


class CountingChannel:
    """Records each ``signal_working`` frame as ``(active, recipient, provider_message_id)``."""

    working_signal_expiry_seconds = 25.0

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[bool, str, str | None]] = []
        self._fail = fail

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))
        if self._fail:
            raise ChannelDeliveryError("vendor rejected the working signal")


class GatedChannel(CountingChannel):
    """Records each frame on arrival; an assert then waits until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.finished: list[bool] = []

    async def signal_working(self, *, recipient, sender_identity=None, provider_message_id=None, active=True) -> None:
        self.calls.append((active, recipient, provider_message_id))
        if active and not self.gate.is_set():
            self.entered.set()
            await self.gate.wait()
        self.finished.append(active)


class _CallRecorder(Protocol):
    calls: list[tuple[bool, str, str | None]]


def actives(channel: _CallRecorder) -> list[tuple[bool, str, str | None]]:
    return [c for c in channel.calls if c[0] is True]


def clears(channel: _CallRecorder) -> list[tuple[bool, str, str | None]]:
    return [c for c in channel.calls if c[0] is False]


class _Channels:
    def __init__(self, channel, *, missing: bool = False) -> None:
        self._channel = channel
        self._missing = missing

    def get(self, name: str):
        if self._missing:
            raise KeyError(name)
        return self._channel


class App:
    def __init__(self, channel, *, missing: bool = False) -> None:
        self.channels = _Channels(channel, missing=missing)


def install_app(monkeypatch, channel, *, missing: bool = False) -> None:
    monkeypatch.setattr(instance_module, "app", App(channel, missing=missing))


class InstantClock:
    """A monotonic clock every sleep advances at once, so a stretch passes without real waits."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.t += max(delay, 0.0)
        await REAL_SLEEP(0)


def install_clock(monkeypatch, clock):
    monkeypatch.setattr(working_signal, "_now", clock.now)
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    return clock


class ManualClock:
    """A monotonic clock only the test moves; a sleep ends once the test advanced past its end."""

    def __init__(self) -> None:
        self.t = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await REAL_SLEEP(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.t + delay, future))
        await future

    async def advance(self, seconds: float) -> None:
        """Move the clock ``seconds`` ahead in one-second steps, letting the loop serve each step."""
        end = self.t + seconds
        while self.t < end:
            self.t = min(self.t + 1.0, end)
            for at, future in list(self._sleepers):
                if at <= self.t and not future.done():
                    future.set_result(None)
            self._sleepers = [(at, f) for at, f in self._sleepers if not f.done()]
            await settle()


async def settle() -> None:
    """Let every ready task run until the loop has nothing left to do without the clock."""
    for _ in range(50):
        await REAL_SLEEP(0)


class SendStore:
    """The conversations records a loop reads: the run's own record and each ask's send record."""

    def __init__(self) -> None:
        self.records: dict[str, ConversationRecord] = {}
        self.ask_sends: dict[str, str] = {}
        self.fail_ask_reads = False
        self.record_reads: list[str] = []

    def ask_on(self, interaction_id: str, client_address: str = USER_ADDRESS) -> None:
        message_id = f"send-{interaction_id}"
        self.ask_sends[interaction_id] = message_id
        self.records[message_id] = record(message_id, client_address=client_address, provider_message_id=None)

    async def ask_send_message_id(self, interaction_id: str) -> str | None:
        if self.fail_ask_reads:
            raise RuntimeError("store is unreachable")
        return self.ask_sends.get(interaction_id)

    async def get_record(self, message_id: str) -> ConversationRecord | None:
        self.record_reads.append(message_id)
        return self.records.get(message_id)


def install_store(monkeypatch, store) -> None:
    manager = durable_manager(records=store)
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)


def install_run_target(monkeypatch, *, gate: asyncio.Event | None = None, fail: bool = False) -> None:
    """Resolve every thread to ``USER_ADDRESS``, after ``gate`` is set when one is given."""

    async def _resolve(thread_id: str):
        if gate is not None:
            await gate.wait()
        if fail:
            raise CompletionDeliveryError(f"no route for {thread_id}")
        return SimpleNamespace(channel=CHANNEL, our_identity=OUR_IDENTITY), USER_ADDRESS

    monkeypatch.setattr(completion_delivery, "resolve_completion_target", _resolve)


async def stop_all() -> None:
    """Stop every loop a test left running and wait for its task."""
    for run_id, signal in list(working_signal._SIGNALS.items()):
        working_signal.stop(run_id)
        if signal.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await signal.task
