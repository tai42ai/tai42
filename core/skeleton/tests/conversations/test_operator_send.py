"""The operator send (turn seam): a message minted already ``answered`` with
``origin="operator"``, appended to an agent target's checkpoint as an assistant reply, and
handed to the delivery machine — sent from the route identity, no turn run.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.caps import ThreadBusyError
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import OperatorAppendError, operator_send

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


class _EchoInput(BaseModel):
    user_message: str = ""


class RecordingAgent(Agent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self, *, append_fails: bool = False) -> None:
        self.runs: list = []
        self.appended: list[tuple[str, list[dict[str, str]]]] = []
        self._append_fails = append_fails

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.runs.append((user_message, thread_id))
        return "unexpected"

    async def append_thread_messages(self, *, thread_id: str, messages, **kwargs) -> None:
        if self._append_fails:
            raise RuntimeError("checkpoint write refused")
        self.appended.append((thread_id, messages))


class MemorylessAgent(Agent):
    """An agent that leaves ``append_thread_messages`` the ABC default (raises
    ``NotImplementedError`` if ever called), so it holds no thread memory to feed."""

    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.runs: list = []

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.runs.append((user_message, thread_id))
        return "unexpected"


class FakeManager:
    def __init__(self, *routes: ConversationRoute) -> None:
        self._routes = {r.route_name: r for r in routes}

    async def list_routes(self):
        return dict(self._routes)

    async def get_route(self, name: str):
        return self._routes.get(name)


class FakeChannel:
    def __init__(self) -> None:
        self.sends: list = []

    async def notify(self, notification):
        self.sends.append(notification)
        return [f"out-{len(self.sends)}"]


class _FakeChannels:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> FakeChannel:
        return self._channel


class _FakeApp:
    def __init__(self, channel: FakeChannel) -> None:
        self.channels = _FakeChannels(channel)


def _channel_route(target_kind: str = "agent") -> ConversationRoute:
    return ConversationRoute(
        route_name="line",
        door="channel",
        target_kind=target_kind,  # pyright: ignore[reportArgumentType]
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
    )


@asynccontextmanager
async def _fake_bind(execution_key, *, bound_fingerprint):
    yield CallerIdentity(user_id=execution_key)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
    for module in (records_module, ledger_module, mode_module, thread_lease_module):
        monkeypatch.setattr(module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(turn_module, "bind_execution_identity", _fake_bind)
    fake.seed_route("line")
    return fake


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


async def _settle(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            if not any(not t.done() for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS)):
                return
        await asyncio.wait(tasks, timeout=0.05)


def _wire(monkeypatch, manager: FakeManager, agent: Agent | None, channel: FakeChannel) -> None:
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))
    if agent is not None:
        monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})


async def test_operator_send_agent_appends_creates_and_delivers(env, monkeypatch):
    agent = RecordingAgent()
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), agent, channel)

    message_id = await operator_send(
        route=route,
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        text="on it",
        operator_principal="op-1",
    )
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert record.answer_status == "answered"
    assert record.answer == "on it"
    assert record.inbound_text == ""
    assert record.caller_principal == "op-1"
    # No turn ran; the operator reply was appended to memory as an assistant message.
    assert agent.runs == []
    assert agent.appended == [("bridge:line:+15550002222", [{"role": "assistant", "content": "on it"}])]
    # Delivered from the ROUTE identity to the client address.
    assert channel.sends[0].sender_identity == "+15550001111"
    assert channel.sends[0].recipient == "+15550002222"
    assert channel.sends[0].message == "on it"
    assert record.delivery_status is DeliveryStatus.DELIVERED


class RichFakeChannel(FakeChannel):
    """A channel that advertises the richer-send capabilities, so the delivery machine sends
    an operator part's media/options instead of refusing them as unrenderable."""

    supports_media_notifications = True
    supports_interactive_notifications = True


async def test_operator_send_with_media_stores_a_rich_part_and_delivers_it(env, monkeypatch):
    from tai42_contract.interactions import MediaItem, MediaKind

    agent = RecordingAgent()
    channel = RichFakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), agent, channel)

    item = MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/receipt.png", caption="receipt")
    message_id = await operator_send(
        route=route,
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        text="here you go",
        operator_principal="op-1",
        media=[item],
        options=["Thanks"],
    )
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    # The legacy joined text is preserved for every plain reader; the rich part carries the
    # media/options the delivery machine sends alongside it.
    assert record.answer == "here you go"
    assert record.answer_parts is not None
    assert record.answer_parts[0].message == "here you go"
    assert record.answer_parts[0].media == [item]
    assert record.answer_parts[0].options == ["Thanks"]
    # The delivered notification carries the operator's media/options (final chunk of the part).
    assert channel.sends[0].message == "here you go"
    assert channel.sends[0].media == [item]
    assert channel.sends[0].options == ["Thanks"]
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_operator_send_plain_text_carries_no_parts(env, monkeypatch):
    # A text-only operator send stays byte-parity with the pre-rich path: no answer_parts.
    channel = FakeChannel()
    route = _channel_route(target_kind="tool")
    _wire(monkeypatch, FakeManager(route), None, channel)

    message_id = await operator_send(
        route=route,
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        text="just text",
        operator_principal="op-1",
    )
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "just text"
    assert record.answer_parts is None


async def test_operator_send_tool_target_appends_nothing(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route(target_kind="tool")
    _wire(monkeypatch, FakeManager(route), None, channel)

    message_id = await operator_send(
        route=route,
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        text="on it",
        operator_principal="op-1",
    )
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert channel.sends[0].message == "on it"


async def test_operator_send_append_failure_raises_and_creates_no_record(env, monkeypatch):
    agent = RecordingAgent(append_fails=True)
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), agent, channel)

    with pytest.raises(OperatorAppendError, match="appending the operator message"):
        await operator_send(
            route=route,
            thread_id="bridge:line:+15550002222",
            client_address="+15550002222",
            text="on it",
            operator_principal="op-1",
        )
    await _settle()

    # Order is append -> create: a failed append leaves nothing in the store and sends nothing.
    assert channel.sends == []
    assert env._hashes == {} or all(not key.startswith(ConversationsSettings().record_key("")) for key in env._hashes)


async def test_operator_send_memoryless_agent_appends_nothing_and_delivers(env, monkeypatch):
    # A memoryless agent target (leaves append_thread_messages the ABC default) has no thread
    # memory to feed, so the operator send skips the append and still creates + delivers the
    # record — no append is attempted (the ABC default would raise) and there is no error.
    agent = MemorylessAgent()
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), agent, channel)

    message_id = await operator_send(
        route=route,
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        text="on it",
        operator_principal="op-1",
    )
    await _settle()

    assert agent.runs == []
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert record.answer == "on it"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends[0].message == "on it"


async def test_operator_send_unregistered_agent_appends_nothing_and_delivers(env, monkeypatch):
    # An unregistered agent target has no thread memory to feed either, so the send skips the
    # append and still creates + delivers the record — no error.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), None, channel)
    monkeypatch.setattr(turn_module, "_agent_registry", dict)

    message_id = await operator_send(
        route=route,
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        text="on it",
        operator_principal="op-1",
    )
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends[0].message == "on it"


async def test_operator_send_waits_behind_an_in_flight_turn(env, monkeypatch):
    # A turn holds the thread's per-thread FIFO lock; the operator send takes the SAME lock, so
    # its append + record write cannot interleave the in-flight turn — it lands only once the
    # turn releases the thread.
    agent = RecordingAgent()
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), agent, channel)
    thread_id = "bridge:line:+15550002222"

    caps = caps_module.get_turn_caps()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_thread() -> None:
        caps.reserve_thread_slot(thread_id)
        async with caps.run_reserved(thread_id):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(_hold_the_thread())
    await asyncio.wait_for(holding.wait(), 1.0)

    send = asyncio.create_task(
        operator_send(
            route=route,
            thread_id=thread_id,
            client_address="+15550002222",
            text="on it",
            operator_principal="op-1",
        )
    )
    # While the turn holds the thread, the send is blocked before its append: nothing written.
    await asyncio.sleep(0.05)
    assert not send.done()
    assert agent.appended == []
    assert channel.sends == []

    # The turn finishes and releases the thread; only now does the operator write proceed.
    release.set()
    await holder
    message_id = await send
    await _settle()

    assert agent.appended == [(thread_id, [{"role": "assistant", "content": "on it"}])]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert channel.sends[0].message == "on it"


async def test_operator_send_refuses_503_when_a_foreign_worker_holds_the_lease(env, monkeypatch):
    # A sibling worker holds the thread's cross-worker lease. Operator send is a live-caller sync
    # door, so its bounded acquisition refuses with the loud, retriable ThreadBusyError (a 503)
    # rather than blocking the caller unbounded behind the other worker's — possibly HITL-paused —
    # turn. Nothing is written and the foreign lease is untouched.
    monkeypatch.setenv("CONVERSATIONS_SYNC_DOOR_WAIT_SECONDS", "0.1")
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    agent = RecordingAgent()
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), agent, channel)
    thread_id = "bridge:line:+15550002222"

    key = ConversationsSettings().thread_lease_key(thread_id)
    await env.set(key, "foreign-token", px=120_000, nx=True)

    with pytest.raises(ThreadBusyError, match="busy with an in-flight turn"):
        await operator_send(
            route=route,
            thread_id=thread_id,
            client_address="+15550002222",
            text="on it",
            operator_principal="op-1",
        )
    await _settle()

    assert agent.appended == []
    assert channel.sends == []
    assert env._strings.get(key) == "foreign-token"
