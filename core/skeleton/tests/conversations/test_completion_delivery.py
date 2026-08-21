"""The completion continuation ``conversation_deliver`` (``deliver_agent_completion``): a
resumed agent turn's FINAL answer minted as an already-terminal record keyed by the stable
completion id and handed to the delivery machine — no turn run, no memory append (the resumed
run already recorded the answer in its own checkpoint). A repeated completion id dedupes to one
record (the lease-lapse re-drive is a benign no-op), a blank answer delivers the client-safe
error reply, and an unresolvable thread raises loudly.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import CompletionDeliveryError, deliver_agent_completion

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


class _EchoInput(BaseModel):
    user_message: str = ""


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


def _channel_route() -> ConversationRoute:
    return ConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
    for module in (records_module, ledger_module, mode_module, thread_lease_module):
        monkeypatch.setattr(module, "client_ctx", make_record_client_ctx(fake))
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


def _wire(monkeypatch, manager: FakeManager, channel: FakeChannel) -> None:
    monkeypatch.setattr(turn_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))


async def test_completion_delivers_the_resumed_answer_into_the_thread(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    out = await deliver_agent_completion("bridge:line:+15550002222", "here is your answer", "cmpl-1")
    await _settle()

    message_id = out["message_id"]
    assert message_id == "cmpl-1"
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert record.answer_status == "answered"
    assert record.answer == "here is your answer"
    assert record.inbound_text == ""
    assert record.caller_principal == "system:agent-resume"
    assert record.route_name == "line"
    assert record.client_address == "+15550002222"
    # Delivered from the ROUTE identity to the client address — no turn ran, no append.
    assert channel.sends[0].sender_identity == "+15550001111"
    assert channel.sends[0].recipient == "+15550002222"
    assert channel.sends[0].message == "here is your answer"
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_completion_serializes_a_structured_result(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    out = await deliver_agent_completion("bridge:line:+15550002222", {"ok": True}, "cmpl-struct")
    await _settle()

    message_id = out["message_id"]
    assert message_id == "cmpl-struct"
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == '{"ok": true}'


async def test_completion_empty_result_delivers_error_reply(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    out = await deliver_agent_completion("bridge:line:+15550002222", "   ", "cmpl-empty")
    await _settle()

    # A resumed run that produced no text delivers the SAME client-safe error reply text the
    # fresh-turn path delivers for an empty answer — never a silent non-delivery. It rides the
    # operator record as an answered reply (an operator record is always answered).
    assert out == {"message_id": "cmpl-empty"}
    record = await _store().get_record("cmpl-empty")
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == "Sorry, something went wrong handling your message. Please try again."
    assert channel.sends[0].message == "Sorry, something went wrong handling your message. Please try again."


async def test_completion_dedupes_on_repeated_completion_id(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    first = await deliver_agent_completion("bridge:line:+15550002222", "the answer", "cmpl-dupe")
    await _settle()
    # A lease-lapse re-drive fires the completion a SECOND time under the same stable id: the
    # durable record already committed, so it is a benign no-op — no second record, no second send.
    second = await deliver_agent_completion("bridge:line:+15550002222", "the answer", "cmpl-dupe")
    await _settle()

    assert first == {"message_id": "cmpl-dupe"}
    assert second == {"message_id": "cmpl-dupe"}
    assert len(channel.sends) == 1


async def test_completion_unresolvable_thread_raises(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    # A non-bridge thread cannot be reversed to a route + address.
    with pytest.raises(CompletionDeliveryError, match="not a reserved bridge thread"):
        await deliver_agent_completion("not-a-bridge-thread", "hi", "cmpl-bad-1")
    # A route-keyed thread whose route no longer exists.
    with pytest.raises(CompletionDeliveryError, match="no longer exists"):
        await deliver_agent_completion("bridge:gone:+15550002222", "hi", "cmpl-bad-2")
    assert channel.sends == []
