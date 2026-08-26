"""The turn engine + delivery integration: accept -> turn (AS the execution key) ->
persist -> deliver, over the channel and API doors, dedupe idempotency, error-outcome
delivery, the sync-wait/callback split with no double-fire, and the startup re-drive.

The execution-identity seam is stubbed here so these isolate the bridge's own behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.conversations import ConversationRoute, DeliveryReceipt
from tai42_contract.interactions import PARK_COMPLETION_FAILED, PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import target_config as target_config_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import PermissionDenied

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


class _EchoInput(BaseModel):
    user_message: str = ""


class EchoAgent(Agent):
    tool_name = "echo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.calls.append((user_message, thread_id))
        return f"echo: {user_message}"


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


def _channel_route(
    route_name: str = "line",
    our_identity: str = "+15550001111",
    *,
    turns_per_hour_override: int | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
        turns_per_hour_override=turns_per_hour_override,
    )


def _api_route(route_name: str = "chat", *, turns_per_hour_override: int | None = None) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
        turns_per_hour_override=turns_per_hour_override,
    )


def _tool_channel_route(
    route_name: str = "tool-line",
    our_identity: str = "+15550001111",
    *,
    target_name: str = "echo-tool",
    payload_expr: str | None = None,
    reply_expr: str | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="tool",
        target_name=target_name,
        payload_expr=payload_expr,
        reply_expr=reply_expr,
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
    )


def _tool_api_route(
    route_name: str = "tool-api",
    *,
    target_name: str = "echo-tool",
    payload_expr: str | None = None,
    reply_expr: str | None = None,
) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="tool",
        target_name=target_name,
        payload_expr=payload_expr,
        reply_expr=reply_expr,
        execution_key="svc",
        callback_url="https://cb.example/x",
        callback_secret="sec-1",
        execution_key_fingerprint="fp-1",
    )


class _FakeTools:
    """A tool registry whose ``run_tool`` returns whatever a supplied callable produces from
    the dispatched kwargs — the stateless dispatch the tool-target turn drives."""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[dict] = []

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False):
        self.calls.append({"key": key, "arguments": arguments, "offload_sync": offload_sync})
        return self.fn(arguments)


def _wire_tool(monkeypatch, fn) -> _FakeTools:
    tools = _FakeTools(fn)
    monkeypatch.setattr(turn_module, "_tools", lambda: tools)
    return tools


@asynccontextmanager
async def _fake_bind(execution_key, *, bound_fingerprint):
    yield CallerIdentity(user_id=execution_key)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(ledger_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(target_config_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(mode_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(thread_lease_module, "client_ctx", make_record_client_ctx(fake))
    # Stub the execution-identity authorization seam so the bridge is tested in isolation.
    monkeypatch.setattr(turn_module, "bind_execution_identity", _fake_bind)

    async def _allow(identity, agent_name, **kwargs):
        return None

    monkeypatch.setattr(turn_module, "authorize_execution_agent_run", _allow)
    return fake


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


async def _settle(timeout: float = 2.0) -> None:
    """Let the spawned turn, delivery and grace tasks run to completion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            if not any(not t.done() for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS)):
                return
        await asyncio.wait(tasks, timeout=0.05)


def _wire(monkeypatch, manager: FakeManager, channel: FakeChannel | None = None) -> None:
    monkeypatch.setattr(turn_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    if channel is not None:
        monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))


# -- channel door ------------------------------------------------------------


async def test_accept_channel_happy_path(env, monkeypatch):
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await turn_module.accept("twilio", "+15550001111", " +15550002222 ", " +15550002222 ", "hi", "PID1")
    await _settle()

    assert agent.calls == [("hi", "bridge:line:+15550002222")]  # canonical address, reserved thread ns
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == "echo: hi"
    # The channel was texted FROM our_identity TO the client address.
    assert channel.sends[0].sender_identity == "+15550001111"
    assert channel.sends[0].recipient == "+15550002222"
    # Provisional then confirmed on grace expiry; the outbound id is indexed.
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert await _store().resolve_outbound("twilio", "out-1") == message_id


async def test_accept_is_idempotent_on_provider_message_id(env, monkeypatch):
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    second = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi again", "PID1")
    await _settle()

    assert first == second
    assert len(agent.calls) == 1  # the redelivery started no second turn


async def test_no_matching_route_raises_loudly(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_channel_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    with pytest.raises(turn_module.ConversationRouteResolutionError):
        await turn_module.accept("twilio", "+19999999999", "+15550002222", "+15550002222", "hi", "PID9")


async def test_two_routes_claiming_one_channel_identity_is_a_loud_corrupt_table(env, monkeypatch):
    # Two channel routes bound to the SAME (channel, our_identity) is a corrupt routing table:
    # the resolver refuses to pick one and raises rather than silently routing to either.
    _wire(monkeypatch, FakeManager(_channel_route("line-a"), _channel_route("line-b")))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    with pytest.raises(RuntimeError, match="routing table is inconsistent"):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PIDX")


async def test_denied_turn_delivers_an_error_outcome(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    async def _deny(identity, agent_name, **kwargs):
        raise PermissionDenied("no run grant")

    monkeypatch.setattr(turn_module, "authorize_execution_agent_run", _deny)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer is not None
    assert "something went wrong" in record.answer.lower()
    assert record.error is not None
    assert "denied" in record.error
    # The error outcome is DELIVERED, not silently dropped.
    assert channel.sends  # a message went out


# -- tool target -------------------------------------------------------------


async def test_tool_target_delivers_its_string_reply(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    tools = _wire_tool(monkeypatch, lambda kw: f"tool saw {kw['message']} from {kw['sender']}")

    message_id = await turn_module.accept("twilio", "+15550001111", " +15550002222 ", " +15550002222 ", "hi", "PID1")
    await _settle()

    # The default kwargs are exactly {message, sender} — canonical address for the sender —
    # and the sync tool is offloaded off the event loop.
    assert tools.calls == [
        {"key": "echo-tool", "arguments": {"message": "hi", "sender": "+15550002222"}, "offload_sync": True}
    ]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == "tool saw hi from +15550002222"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == ["tool saw hi from +15550002222"]


@pytest.mark.parametrize("reply", [None, "", "   "])
async def test_tool_target_none_or_blank_reply_is_silent(env, monkeypatch, reply):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: reply)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    # Terminal SILENT: no answer, no answer_status, nothing sent — by design, not an error.
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer is None
    assert record.answer_status is None
    assert channel.sends == []


async def test_tool_target_payload_expr_maps_the_kwargs(env, monkeypatch):
    # A preset-shaped payload: an expr emitting {"example_config_kwargs": {...}}.
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr="{example_config_kwargs: {text: .message, from: .sender}}")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "run it", "PID1")
    await _settle()

    assert tools.calls[0]["arguments"] == {"example_config_kwargs": {"text": "run it", "from": "+15550002222"}}


async def test_tool_target_reply_expr_over_an_envelope_dict(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"result": {"reply": "from the envelope"}})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "from the envelope"
    assert [n.message for n in channel.sends] == ["from the envelope"]


async def test_tool_target_reply_expr_yielding_null_is_silent(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"result": {}})  # .reply // null -> null

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []


async def test_tool_target_suspended_interaction_ends_the_turn_silently(env, monkeypatch):
    # A tool that async-parks the caller returns the generic SuspendedInteraction
    # sentinel; the turn recognizes it by TYPE (never a reply_expr mapping) and ends
    # silently — the parked continuation resumes work out of band.
    from tai42_contract.interactions import SuspendedInteraction

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: SuspendedInteraction(interaction_id="i-async"))

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert record.answer is None
    assert record.answer_status is None
    assert channel.sends == []


async def test_tool_route_park_binds_completion_and_delivers_via_reply_expr(env, monkeypatch):
    # R1+D1: a conversation route to a GENERIC parking tool binds the generic tool-route
    # completion (naming THIS thread as the opaque delivery address) around the dispatch and
    # parks silently; when the tool's own resumer later fires deliver_tool_completion with the
    # terminal outcome, the route's reply_expr maps it and it is delivered back into the thread,
    # idempotent on completion_id.
    from tai42_contract.interactions import SuspendedInteraction, get_park_completion

    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    bound: list = []

    def _park(kw):
        bound.append(get_park_completion())
        return SuspendedInteraction(interaction_id="i-async")

    _wire_tool(monkeypatch, _park)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    # The dispatch parked silently; the completion was bound naming deliver_tool_completion and
    # this turn's thread as the opaque delivery address.
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.SILENT
    assert channel.sends == []
    assert len(bound) == 1
    tool_name, context = bound[0]
    assert tool_name == turn_module.DELIVER_TOOL_COMPLETION_NAME
    thread_id = context["delivery_thread_id"]
    assert thread_id == "bridge:tool-line:+15550002222"
    # The ORIGINATING route is pinned into the completion context so the resumed outcome maps
    # through THIS route's reply_expr, not the route a linked person last wrote from.
    assert context["route_name"] == "tool-line"

    # The resumer drives to a clean terminal out of band and fires the completion; reply_expr
    # maps the terminal outcome and it is delivered back into the thread.
    out = await turn_module.deliver_tool_completion(
        delivery_thread_id=thread_id,
        completion_id="c1",
        result={"result": {"reply": "the deferred answer"}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": "c1"}
    delivered = await _store().get_record("c1")
    assert delivered is not None
    assert delivered.answer == "the deferred answer"
    assert delivered.delivery_status is DeliveryStatus.DELIVERED
    assert "the deferred answer" in [n.message for n in channel.sends]

    # Idempotent: a redelivered fire under the same completion_id delivers nothing new.
    sends_before = len(channel.sends)
    out2 = await turn_module.deliver_tool_completion(
        delivery_thread_id=thread_id,
        completion_id="c1",
        result={"result": {"reply": "SECOND"}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out2 == {"message_id": "c1"}
    assert len(channel.sends) == sends_before


async def test_deliver_tool_completion_non_success_delivers_error_notice(env, monkeypatch):
    # A non-success terminal (the route carries no error mapping) delivers the uniform
    # client-safe notice — never the raw internal detail, never a silent drop.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    out = await turn_module.deliver_tool_completion(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="e1",
        result={"detail": "internal"},
        status=PARK_COMPLETION_FAILED,
    )
    await _settle()
    assert out == {"message_id": "e1"}
    rec = await _store().get_record("e1")
    assert rec is not None
    assert rec.answer == turn_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_deliver_tool_completion_unmappable_success_delivers_error_notice(env, monkeypatch):
    # A success terminal the route's reply_expr cannot map is delivered as the client-safe
    # notice rather than crashing the resumer or dropping the outcome.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply")  # a non-string/non-null emit raises
    _wire(monkeypatch, FakeManager(route), channel)

    out = await turn_module.deliver_tool_completion(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="u1",
        result={"result": {"reply": {"not": "a string"}}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": "u1"}
    rec = await _store().get_record("u1")
    assert rec is not None
    assert rec.answer == turn_module._ERROR_ANSWER_TEXT


async def test_deliver_tool_completion_silent_reply_delivers_nothing(env, monkeypatch):
    # A success whose reply_expr maps to null is a designed silent outcome: nothing delivered,
    # and — naturally idempotent — no record anchors it.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    out = await turn_module.deliver_tool_completion(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="s1",
        result={"result": {}},
        status=PARK_COMPLETION_SUCCEEDED,
    )
    await _settle()
    assert out == {"message_id": None}
    assert await _store().get_record("s1") is None
    assert channel.sends == []


async def test_deliver_tool_completion_unresolvable_thread_raises(env, monkeypatch):
    # An unresolvable delivery address raises loudly so the resumer's at-least-once seam
    # retains and retries rather than dropping the outcome.
    _wire(monkeypatch, FakeManager(_tool_channel_route()))
    with pytest.raises(turn_module.CompletionDeliveryError):
        await turn_module.deliver_tool_completion(
            delivery_thread_id="not-a-bridge-thread", completion_id="x1", result="hi"
        )


async def test_deliver_tool_completion_defaults_to_the_failed_fail_safe(env, monkeypatch):
    # FAIL-SAFE: an unstamped fire (no status) defaults to FAILED, so it delivers the client-safe
    # notice and NEVER pushes an unmapped payload through reply_expr as if it had succeeded.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    out = await turn_module.deliver_tool_completion(
        delivery_thread_id="bridge:tool-line:+15550002222",
        completion_id="d1",
        result={"result": {"reply": "would-map-if-success"}},
    )
    await _settle()
    assert out == {"message_id": "d1"}
    rec = await _store().get_record("d1")
    assert rec is not None
    # Delivered the notice, NOT the reply_expr-mapped success text.
    assert rec.answer == turn_module._ERROR_ANSWER_TEXT


async def test_deliver_tool_completion_maps_via_the_pinned_originating_route(env, monkeypatch):
    # FINDING 3: a park started under route A; the linked person then wrote from route B, so
    # _resolve_completion_target reverses the thread to route B (where they last wrote). The
    # completion must still map the terminal through the ORIGINATING route A's reply_expr — not
    # route B's, which would map the SAME result wrongly. Delivery still lands on route B's
    # address (where the person is), but the mapped text comes from A.
    channel = FakeChannel()
    route_a = _tool_channel_route(route_name="tool-a", reply_expr=".a.text")
    route_b = _tool_channel_route(route_name="tool-b", our_identity="+15550009999", reply_expr=".b.text")
    _wire(monkeypatch, FakeManager(route_a, route_b), channel)

    # Simulate the linked person having last written from route B: the thread reverses to B.
    async def _resolve_to_b(_thread_id):
        return route_b, "+15550002222"

    monkeypatch.setattr(turn_module, "_resolve_completion_target", _resolve_to_b)

    out = await turn_module.deliver_tool_completion(
        delivery_thread_id="bridge:@person:PID1",
        completion_id="p1",
        result={"a": {"text": "via-A"}, "b": {"text": "via-B"}},
        status=PARK_COMPLETION_SUCCEEDED,
        route_name="tool-a",
    )
    await _settle()
    assert out == {"message_id": "p1"}
    rec = await _store().get_record("p1")
    assert rec is not None
    # Mapped through route A's reply_expr (the originating route), NOT route B's.
    assert rec.answer == "via-A"
    # Delivered where the person last wrote (route B's reversed address).
    assert rec.route_name == "tool-b"


async def test_deliver_tool_completion_raises_on_vanished_originating_route(env, monkeypatch):
    # The pinned originating route no longer exists (deleted between park and resume): raise
    # loudly so the resumer retries rather than mapping through the wrong route.
    channel = FakeChannel()
    route = _tool_channel_route(route_name="tool-live", reply_expr=".result.reply // null")
    _wire(monkeypatch, FakeManager(route), channel)

    with pytest.raises(turn_module.CompletionDeliveryError):
        await turn_module.deliver_tool_completion(
            delivery_thread_id="bridge:tool-live:+15550002222",
            completion_id="v1",
            result={"result": {"reply": "x"}},
            status=PARK_COMPLETION_SUCCEEDED,
            route_name="tool-gone",
        )


async def test_tool_target_wrong_typed_result_is_an_error(env, monkeypatch):
    # No reply_expr, so a non-null/non-string result is a turn error, not a silent drop.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: {"unexpected": "dict"})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_tool_target_that_raises_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)

    def _boom(kw):
        raise RuntimeError("tool blew up")

    _wire_tool(monkeypatch, _boom)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "tool blew up" in record.error
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_tool_target_payload_expr_multi_emit_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".message, .sender")  # two emits
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "unreachable")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls == []  # the tool never ran
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_tool_target_payload_expr_non_object_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".message")  # emits a string, not an object
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "unreachable")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls == []  # the mapping failed before the tool ran
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_tool_target_reply_expr_multi_emit_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".a, .b")  # two emits
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"a": "one", "b": "two"})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_tool_target_reply_expr_non_string_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".count")  # emits a number
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"count": 42})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_tool_target_denied_dispatch_is_an_error(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: "unreachable")

    @asynccontextmanager
    async def _deny_bind(execution_key, *, bound_fingerprint):
        raise PermissionDenied("the execution key carries no authority")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(turn_module, "bind_execution_identity", _deny_bind)

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "denied" in record.error
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]


async def test_blank_inbound_text_is_refused_before_any_state(env, monkeypatch):
    from tai42_contract.conversations import BlankInboundTextError

    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    for blank in ("", "   ", "\t\n"):
        with pytest.raises(BlankInboundTextError):
            await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", blank, "PID1")
    await _settle()

    assert await _store().list_by_status(frozenset(DeliveryStatus)) == []


async def test_a_blank_accountable_cap_key_is_refused_loudly(env, monkeypatch):
    # A door that omits the accountable key must fail at the seam, never fall back to
    # sharing one bucket — a blank cap_key raises rather than resolving.
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="non-blank"):
            await turn_module.accept("twilio", "+15550001111", "+15550002222", blank, "hi", "PID1")
    await _settle()

    assert await _store().list_by_status(frozenset(DeliveryStatus)) == []


async def test_tool_target_dispatch_is_offloaded_off_the_event_loop(env, monkeypatch):
    # A synchronous tool must run off the loop, or a blocking dispatch starves the turn
    # engine; the bridge always dispatches with offload_sync set.
    _wire(monkeypatch, FakeManager(_tool_channel_route()), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls[0]["offload_sync"] is True


async def test_tool_target_payload_expr_reads_our_identity_and_channel(env, monkeypatch):
    # our_identity and channel reach the tool through payload_expr; on the channel door they
    # carry the route's real values.
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr="{oid: .our_identity, ch: .channel}")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert tools.calls[0]["arguments"] == {"oid": "+15550001111", "ch": "twilio"}


async def test_tool_target_payload_expr_our_identity_and_channel_are_null_on_the_api_door(env, monkeypatch):
    # An api route carries no channel/our_identity, so both read as null through payload_expr.
    route = _tool_api_route(payload_expr="{oid: .our_identity, ch: .channel}")
    _wire(monkeypatch, FakeManager(route))
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=5)
    await _settle()

    assert tools.calls[0]["arguments"] == {"oid": None, "ch": None}


async def test_tool_target_over_the_api_door_silent_delivers_a_signed_silent_callback(env, monkeypatch):
    # A silent api-door tool turn keeps its 202 promise through a signed callback carrying
    # the explicit silent marker and NO answer field.
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    _wire_tool(monkeypatch, lambda kw: None)
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature, json.loads(body)))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=0)
    assert result.answer is None  # 202: the outcome is delivered out of band
    await _settle()

    assert len(posted) == 1  # exactly one callback, no double-fire
    url, signature, body = posted[0]
    assert url == "https://cb.example/x"
    assert signature.startswith("sha256=")
    assert body == {"message_id": result.message_id, "thread_id": result.thread_id, "status": "silent"}
    assert "answer" not in body
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.answer_status == "silent"
    assert record.answer is None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_tool_target_over_the_api_door_silent_sync_wait_returns_the_marker(env, monkeypatch):
    # A silent turn finishing inside the sync wait returns the silent marker inline (200)
    # with its callback suppressed, exactly as an answered turn is.
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    _wire_tool(monkeypatch, lambda kw: None)
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=5)
    await _settle()

    assert result.answer is not None
    assert result.answer.status == "silent"
    assert result.answer.answer is None
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert posted == []  # the sync wait delivered it; no callback fired


# -- entry params reach the tool payload -------------------------------------

# The channel-door tool payload with no params — the byte-identical baseline every
# ``None``/empty-params turn must reproduce exactly (``payload_expr="."`` passes it whole).
_BASELINE_CHANNEL_PAYLOAD = {
    "message": "hi",
    "sender": "+15550002222",
    "our_identity": "+15550001111",
    "channel": "twilio",
}


async def test_tool_payload_carries_params_when_passed(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".")  # pass the whole payload through as kwargs
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", {"token": "abc-123"}
    )
    await _settle()

    assert tools.calls[0]["arguments"] == {**_BASELINE_CHANNEL_PAYLOAD, "params": {"token": "abc-123"}}


@pytest.mark.parametrize("params", [None, {}])
async def test_tool_payload_omits_params_when_none_or_empty(env, monkeypatch, params):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", params)
    await _settle()

    # Byte-identical to today's payload: no ``params`` key at all.
    assert tools.calls[0]["arguments"] == _BASELINE_CHANNEL_PAYLOAD
    assert "params" not in tools.calls[0]["arguments"]


async def test_a_param_named_like_a_root_field_stays_under_params_only(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept(
        "twilio",
        "+15550001111",
        "+15550002222",
        "+15550002222",
        "hi",
        "PID1",
        {"sender": "spoofed", "message": "spoofed", "channel": "spoofed"},
    )
    await _settle()

    seen = tools.calls[0]["arguments"]
    # The root fields keep their real values; the same-named params live ONLY under params —
    # a param can never shadow a platform-set root field.
    assert seen["message"] == "hi"
    assert seen["sender"] == "+15550002222"
    assert seen["channel"] == "twilio"
    assert seen["params"] == {"sender": "spoofed", "message": "spoofed", "channel": "spoofed"}


async def test_api_path_params_reach_the_tool_payload(env, monkeypatch):
    route = _tool_api_route(payload_expr=".")
    _wire(monkeypatch, FakeManager(route))
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", 5, {"token": "xyz"})
    await _settle()

    assert tools.calls[0]["arguments"]["params"] == {"token": "xyz"}


async def test_agent_target_ignores_params(env, monkeypatch):
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", {"token": "abc"}
    )
    await _settle()

    # accept() takes params for every target, but the agent turn receives exactly the raw
    # text and nothing else — no params reach the agent.
    assert agent.calls == [("hi", "bridge:line:+15550002222")]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hi"


async def test_payload_mapping_failure_is_value_free_in_log_and_record(env, monkeypatch, caplog):
    channel = FakeChannel()
    # A jq runtime error (string + number) whose text embeds the offending input value; the
    # platform's mapping-error path must persist and log the error CLASS only.
    route = _tool_channel_route(payload_expr=".params.secret + 1")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "unreachable")

    secret = "super-secret-param-value"
    with caplog.at_level(logging.ERROR):
        message_id = await turn_module.accept(
            "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", {"secret": secret}
        )
        await _settle()

    assert tools.calls == []  # the mapping failed before the tool ran
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    # The persisted detail names the error class, never the offending param value.
    assert secret not in record.error
    # No captured log line carries the value either.
    assert caplog.records  # the failure WAS logged (not swallowed)
    assert all(secret not in rec.getMessage() for rec in caplog.records)


# -- API door ----------------------------------------------------------------


async def test_api_wait_fast_returns_answer_and_suppresses_callback(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("chat", "user-7", "hello", "alice", wait_seconds=5)
    await _settle()

    assert result.answer is not None
    assert result.answer.answer == "echo: hello"
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.caller_principal == "alice"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    # The sync-wait delivered it, so NO callback was POSTed (no double-fire).
    assert posted == []


async def test_api_no_wait_returns_202_then_posts_signed_callback(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("chat", "user-7", "hello", "alice", wait_seconds=0)
    assert result.answer is None  # 202
    await _settle()

    assert len(posted) == 1
    assert posted[0][0] == "https://cb.example/x"
    assert posted[0][1].startswith("sha256=")
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_api_slow_wait_returns_202_then_posts_the_callback(env, monkeypatch):
    # A turn that does NOT finish inside the wait window falls back to 202 and its answer
    # is delivered by exactly one callback — the timeout arm of the wait, then the
    # not-yet-done arm of _deliver_when_done, with no double-fire against the sweep.
    _wire(monkeypatch, FakeManager(_api_route()))
    release = asyncio.Event()

    class _SlowAgent(Agent):
        tool_name = "slow"
        ToolInput = _EchoInput

        async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
            await release.wait()
            return f"echo: {user_message}"

    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": _SlowAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("chat", "user-7", "hello", "alice", wait_seconds=1)
    assert result.answer is None  # the wait elapsed before the turn finished: 202

    release.set()
    await _settle()

    assert posted == ["https://cb.example/x"]  # exactly one callback, no double-fire
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_api_callback_retries_then_fails(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "0.01")
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 500

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message("chat", "user-7", "hello", "alice", wait_seconds=0)
    await _settle()

    assert len(posted) == 2  # exhausted delivery_max_attempts
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.FAILED


# -- API-door identity: the caller qualifies the thread and owns the cap ------


async def test_a_caller_cannot_reach_another_callers_thread_by_naming_its_end_user(env, monkeypatch):
    # The api door's end-user id is caller-asserted, so the thread it keys must also carry
    # the AUTHENTICATED caller: otherwise one caller reads another's conversation memory
    # back out of the agent's own answer.
    agent = MemoryAgent()
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    alice = await turn_module.submit_api_message("chat", "shared-user", "my lucky number is 4111", "alice", 5)
    await _settle()
    bob = await turn_module.submit_api_message("chat", "shared-user", "what did I say?", "bob", 5)
    await _settle()

    assert alice.thread_id != bob.thread_id
    assert alice.thread_id == "bridge:chat:alice/shared-user"
    assert bob.thread_id == "bridge:chat:bob/shared-user"
    # Two threads means two memories: bob's turn never saw alice's message.
    assert sorted(agent.threads) == [alice.thread_id, bob.thread_id]
    assert bob.answer is not None
    assert bob.answer.answer is not None
    assert "4111" not in bob.answer.answer
    # The record's address matches the thread it ran on, so the two never disagree.
    record = await _store().get_record(bob.message_id)
    assert record is not None
    assert record.client_address == "bob/shared-user"


def test_the_api_address_join_is_unambiguous_for_every_principal():
    # The principal is percent-encoded, so no principal/end-user pair can spell the same
    # address slot as a different pair — the separator is not forgeable.
    compose = turn_module._api_client_address
    assert compose("a/b", "c") != compose("a", "b/c")
    assert compose("a%2Fb", "c") != compose("a/b", "c")
    assert compose("a:b", "c") != compose("a", "b:c")


async def test_an_api_caller_cannot_outrun_its_cap_by_varying_the_end_user_id(env, monkeypatch):
    # The cap keys on the caller, not on the caller-chosen end-user id, so minting a fresh
    # id per message buys no extra turns.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "2")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    store = _store()

    for index in range(2):
        await turn_module.submit_api_message("chat", f"u-{index}", "hi", "alice", 0)
    await _settle()

    with pytest.raises(caps_module.AddressRateLimitedError, match="alice"):
        await turn_module.submit_api_message("chat", "u-2", "hi", "alice", 0)
    # The refusal wrote nothing: only the two admitted messages left records.
    assert len(await _all_record_ids(store)) == 2

    # A different caller has its own budget.
    await turn_module.submit_api_message("chat", "u-0", "hi", "bob", 0)
    await _settle()
    assert len(await _all_record_ids(store)) == 3


async def test_an_api_caller_cannot_drain_a_channel_addresss_bucket(env, monkeypatch):
    # The api door's bucket names the caller and the channel door's names the route and
    # the provider-attested address, so an authed caller cannot spend a phone user's budget.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(), _api_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # The caller spends its own single token naming the phone number as its end user.
    await turn_module.submit_api_message("chat", "+15550002222", "hi", "alice", 0)
    await _settle()
    with pytest.raises(caps_module.AddressRateLimitedError):
        await turn_module.submit_api_message("chat", "+15550002222", "again", "alice", 0)

    # The real phone user is untouched: it is admitted and answered.
    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hello", "PID1")
    await _settle()
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hello"
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_two_routes_sharing_an_address_have_independent_buckets(env, monkeypatch):
    # The bucket key carries the route, so draining one route's budget for an address
    # leaves the same address on another route fully funded.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    other = _channel_route(route_name="line-b", our_identity="+15550009999")
    _wire(monkeypatch, FakeManager(_channel_route(), other), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await _settle()
    # The address is now over its cap ON THAT ROUTE: the next message buys a slow-down reply.
    shed = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()
    shed_record = await store.get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == turn_module._SLOW_DOWN_TEXT

    # The same address on the other route still gets its own turn.
    admitted = await turn_module.accept("twilio", "+15550009999", "+15550002222", "+15550002222", "three", "PID3")
    await _settle()
    record = await store.get_record(admitted)
    assert record is not None
    assert record.answer == "echo: three"
    assert record.thread_id == "bridge:line-b:+15550002222"


async def test_two_addresses_sharing_a_cap_key_share_one_turn_bucket(env, monkeypatch):
    # The bridge half of the web rotate-reset defense: two DIFFERENT conversation
    # identities that name the SAME accountable cap key share ONE turn bucket, so a
    # second identity minted under that key cannot buy itself a fresh budget.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    first = await turn_module.accept("twilio", "+15550001111", "visitor-A", "net-bucket", "one", "PID1")
    await _settle()
    first_record = await store.get_record(first)
    assert first_record is not None
    assert first_record.answer == "echo: one"

    # A DIFFERENT identity under the SAME cap key is already over the shared cap: it sheds.
    shed = await turn_module.accept("twilio", "+15550001111", "visitor-B", "net-bucket", "two", "PID2")
    await _settle()
    shed_record = await store.get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == turn_module._SLOW_DOWN_TEXT
    # ...and the two really are distinct conversations.
    assert first_record.client_address == "visitor-A"
    assert shed_record.client_address == "visitor-B"


async def test_two_api_routes_give_a_caller_independent_buckets(env, monkeypatch):
    # The api bucket key carries the route, so a caller draining its cap on one route
    # still has a full budget on another — the route qualifier the caller-scoped fix added.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_api_route("chat"), _api_route("account")))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message("chat", "u-1", "hi", "alice", 0)
    await _settle()
    # alice has spent her single token on ``chat``; a second there is refused.
    with pytest.raises(caps_module.AddressRateLimitedError, match="chat"):
        await turn_module.submit_api_message("chat", "u-2", "hi", "alice", 0)

    # ``account``'s budget for alice is untouched: her message there is admitted and answered.
    result = await turn_module.submit_api_message("account", "u-1", "hi", "alice", 5)
    await _settle()
    assert result.answer is not None
    assert result.answer.answer == "echo: hi"


async def test_an_api_route_override_raises_the_cap_and_the_503_quotes_it(env, monkeypatch):
    # A route carrying ``turns_per_hour_override`` runs its callers at that rate, above the
    # global cap of 1, and the refusal names the EFFECTIVE (override) rate, not the global.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_api_route("chat", turns_per_hour_override=3)))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # The override grants three turns where the global cap would have allowed one.
    for index in range(3):
        await turn_module.submit_api_message("chat", f"u-{index}", "hi", "alice", 0)
    await _settle()

    # The fourth is refused, and the 503 quotes the override rate of 3/hour, not the global 1.
    with pytest.raises(caps_module.AddressRateLimitedError, match="3/hour"):
        await turn_module.submit_api_message("chat", "u-3", "hi", "alice", 0)


async def test_a_channel_route_override_raises_the_cap_through_the_accept_path(env, monkeypatch):
    # The channel door honours ``turns_per_hour_override`` the same way the api door does: an
    # address on an overridden channel route is admitted beyond the global budget, and the
    # paid slow-down reply fires only once the OVERRIDE budget (not the global one) is spent.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(turns_per_hour_override=3)), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    # The override grants three turns where the global cap of 1 would have shed the second.
    for index in range(3):
        admitted = await turn_module.accept(
            "twilio", "+15550001111", "+15550002222", "+15550002222", f"msg-{index}", f"PID{index}"
        )
        await _settle()
        record = await store.get_record(admitted)
        assert record is not None
        assert record.answer == f"echo: msg-{index}"

    # Only the fourth message — past the override budget, not the global one — buys the
    # slow-down reply.
    shed = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "over", "PID9")
    await _settle()
    shed_record = await store.get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == turn_module._SLOW_DOWN_TEXT


@pytest.mark.parametrize("principal", [None, "", "   "])
async def test_an_api_message_without_an_authenticated_caller_is_refused(env, monkeypatch, principal):
    # Keying a thread or a bucket on an absent principal would pool every anonymous caller
    # into one shared conversation, so the door refuses instead.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    with pytest.raises(turn_module.UnauthenticatedApiCallerError):
        await turn_module.submit_api_message("chat", "u-7", "hi", principal, 5)
    await _settle()

    assert agent.calls == []
    assert await _all_record_ids(_store()) == []
    assert caps_module.get_turn_caps()._thread_waiters == {}


def _accepting_callback():
    async def _post(url, body, signature, timeout_seconds):
        return 200

    return _post


# -- exactly-once + re-drive -------------------------------------------------


async def test_record_delivery_status_confirms_provisional(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "3600")  # keep it provisional
    caps_module._CAPS_CACHE.clear()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    # Let the turn + channel send run, but not the (1h) grace confirm.
    for _ in range(50):
        record = await _store().get_record(message_id)
        if record is not None and record.delivery_status is DeliveryStatus.PROVISIONAL:
            break
        await asyncio.sleep(0.01)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.PROVISIONAL

    # A positive out-of-band receipt confirms it delivered.
    await delivery_module.record_delivery_status("twilio", "out-1", DeliveryReceipt.DELIVERED)
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED

    # An unknown outbound id is a loud lookup failure.
    with pytest.raises(LookupError):
        await delivery_module.record_delivery_status("twilio", "nope", DeliveryReceipt.DELIVERED)


async def test_redrive_resumes_a_stranded_pending_record(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    # Simulate a record persisted before a crash: pending_delivery, never sent.
    now = time.time()
    from tai42_skeleton.conversations.models import ConversationRecord

    record = ConversationRecord(
        message_id="stranded",
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask stranded",
        answer_status="answered",
        answer="resumed",
        created_at=now,
        updated_at=now,
    )
    await _store().create_record(record)

    await delivery_module.redrive_pending()
    await _settle()

    assert channel.sends  # the stranded record was re-driven and sent
    got = await _store().get_record("stranded")
    assert got is not None
    assert got.delivery_status in (DeliveryStatus.PROVISIONAL, DeliveryStatus.DELIVERED)


def _answered_channel_record(message_id: str):
    """A channel record carrying its produced answer — the shape the delivery machine
    picks up, whatever state a test then moves it to."""
    from tai42_skeleton.conversations.models import ConversationRecord

    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask already out",
        answer_status="answered",
        answer="already out",
        created_at=now,
        updated_at=now,
    )


def _set_grace_deadline(fake: FakeRecordRedis, message_id: str, deadline: float) -> None:
    fake._hashes[ConversationsSettings().record_key(message_id)]["grace_deadline"] = str(deadline)


async def test_redrive_confirms_a_provisional_record_whose_grace_elapsed(env, monkeypatch):
    # Provisional with its grace already elapsed: boot confirms it and must NOT re-send
    # an answer the medium already took.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    store = _store()
    await store.create_record(_answered_channel_record("prov-elapsed"))
    await store.mark_provisional("prov-elapsed", ["out-1"], 1, time.time(), "tok")
    _set_grace_deadline(env, "prov-elapsed", time.time() - 1)

    await delivery_module.redrive_pending()
    await _settle()

    record = await store.get_record("prov-elapsed")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends == []


async def test_redrive_reschedules_only_the_remaining_grace_of_a_provisional_record(env, monkeypatch):
    # Still inside its window: the fallback is rebuilt for what is LEFT of the grace,
    # not a fresh full window.
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "3600")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    store = _store()
    await store.create_record(_answered_channel_record("prov-young"))
    await store.mark_provisional("prov-young", ["out-1"], 1, time.time(), "tok")
    _set_grace_deadline(env, "prov-young", time.time() + 0.05)

    await delivery_module.redrive_pending()
    still_provisional = await store.get_record("prov-young")
    assert still_provisional is not None
    assert still_provisional.delivery_status is DeliveryStatus.PROVISIONAL

    await _settle()

    record = await store.get_record("prov-young")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends == []


async def test_redrive_races_executor_delivers_once(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    now = time.time()
    from tai42_skeleton.conversations.models import ConversationRecord

    record = ConversationRecord(
        message_id="race",
        route_name="chat",
        door="api",
        thread_id="bridge:chat:user-7",
        client_address="user-7",
        callback_url="https://cb.example/x",
        caller_principal="alice",
        origin="client",
        inbound_text="ask once",
        answer_status="answered",
        answer="once",
        created_at=now,
        updated_at=now,
    )
    await _store().create_record(record)

    # Hold BOTH workers at the claim so each reads the record before either writes a
    # lease; the loser must find a live lease and send nothing.
    at_the_claim = asyncio.Barrier(2)
    claim_delivery = records_module.ConversationRecordStore.claim_delivery

    async def _claim_together(self, message_id, now_, token, lease_seconds):
        await asyncio.wait_for(at_the_claim.wait(), 2)
        return await claim_delivery(self, message_id, now_, token, lease_seconds)

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_delivery", _claim_together)

    # A re-drive and a direct executor race the same record; the atomic claim lets exactly
    # one deliver.
    await asyncio.gather(delivery_module.deliver("race"), delivery_module.deliver("race"))
    await _settle()

    assert len(posted) == 1
    delivered = await _store().get_record("race")
    assert delivered is not None
    assert delivered.delivery_status is DeliveryStatus.DELIVERED


async def test_the_wait_paths_claim_locks_out_a_racing_callback_delivery(env, monkeypatch):
    # The wait path is held between taking its claim and writing the terminal state —
    # the window where only the lease stands between the record and a second delivery.
    _wire(monkeypatch, FakeManager(_api_route()))
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    now = time.time()
    from tai42_skeleton.conversations.models import ConversationRecord

    await _store().create_record(
        ConversationRecord(
            message_id="wait-race",
            route_name="chat",
            door="api",
            thread_id="bridge:chat:user-7",
            client_address="user-7",
            callback_url="https://cb.example/x",
            caller_principal="alice",
            origin="client",
            inbound_text="ask once",
            answer_status="answered",
            answer="once",
            created_at=now,
            updated_at=now,
        )
    )

    claimed = asyncio.Event()
    finish = asyncio.Event()
    mark_delivered = records_module.ConversationRecordStore.mark_delivered

    async def _hold_after_the_claim(self, message_id, outbound_ids, attempts, now_, token):
        claimed.set()
        await finish.wait()
        return await mark_delivered(self, message_id, outbound_ids, attempts, now_, token)

    monkeypatch.setattr(records_module.ConversationRecordStore, "mark_delivered", _hold_after_the_claim)

    waiter = asyncio.create_task(delivery_module.mark_wait_delivered("wait-race"))
    await asyncio.wait_for(claimed.wait(), 2)
    held = await _store().get_record("wait-race")
    assert held is not None
    assert held.delivery_status is DeliveryStatus.PENDING_DELIVERY  # nothing terminal yet

    await delivery_module.deliver("wait-race")
    assert posted == []  # the live lease refused the callback

    finish.set()
    assert await waiter is True
    record = await _store().get_record("wait-race")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert posted == []


# -- thread continuity: two messages share the reserved thread (memory) -------


class MemoryAgent(Agent):
    """An agent that accumulates the messages it has seen per ``thread_id`` and answers
    with the running history — so a second turn on the same thread proves the bridge
    handed it the SAME ``thread_id`` (the memory key)."""

    tool_name = "memo"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.threads: dict[str, list[str]] = {}

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        history = self.threads.setdefault(thread_id or "", [])
        history.append(user_message)
        return " | ".join(history)


async def test_two_messages_from_one_address_remember_via_the_thread_id(env, monkeypatch):
    agent = MemoryAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID-A")
    await _settle()
    second = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID-B")
    await _settle()

    # Both turns ran on the one reserved thread id, so the agent's per-thread memory
    # saw both messages in order — the continuity the bridge owns.
    assert list(agent.threads) == ["bridge:line:+15550002222"]
    assert agent.threads["bridge:line:+15550002222"] == ["one", "two"]
    second_record = await _store().get_record(second)
    assert second_record is not None
    assert second_record.answer == "one | two"
    assert first != second


# -- delivery-time long-answer splitting (D-delivery) -------------------------


async def test_a_long_answer_is_split_into_ordered_channel_sends(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    # Past twilio's 1600 cap: several ordered chunks concatenating to the whole answer,
    # never a silent truncation.
    long_text = "x" * 4000
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": _fixed_answer_agent(long_text)})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID-L")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert len(channel.sends) > 1
    assert "".join(n.message for n in channel.sends) == record.answer
    assert all(len(n.message) <= 1600 for n in channel.sends)


def _fixed_answer_agent(answer: str) -> Agent:
    class _Fixed(Agent):
        tool_name = "fixed"
        ToolInput = _EchoInput

        async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
            return answer

    return _Fixed()


# -- a channel that cannot be sent on is a loud, VISIBLE config error ---------


async def test_missing_channel_in_the_length_map_is_a_loud_failure(env, monkeypatch):
    from tai42_skeleton.conversations.models import ConversationRecord

    _wire(monkeypatch, FakeManager(), FakeChannel())
    now = time.time()
    record = ConversationRecord(
        message_id="unmapped",
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="not-in-the-map",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask hello",
        answer_status="answered",
        answer="hello",
        created_at=now,
        updated_at=now,
    )
    await _store().create_record(record)

    with pytest.raises(RuntimeError, match="no max_message_chars entry"):
        await delivery_module.deliver("unmapped")
    # The record is marked failed so the misconfiguration is operationally visible,
    # not left dangling in pending_delivery.
    got = await _store().get_record("unmapped")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.FAILED


async def test_a_routed_channel_that_is_not_registered_is_a_loud_failure(env, monkeypatch):
    # An unloaded channel plugin can never be delivered by retrying, so the record
    # reaches ``failed`` instead of being re-driven by every sweep forever.
    from tai42_skeleton.conversations.models import ConversationRecord

    class _NoChannels:
        def get(self, name: str):
            raise KeyError(f"unknown channel {name!r} (registered: [])")

    class _AppWithoutTheChannel:
        channels = _NoChannels()

    _wire(monkeypatch, FakeManager())
    monkeypatch.setattr(delivery_module, "tai42_app", _AppWithoutTheChannel())
    now = time.time()
    await _store().create_record(
        ConversationRecord(
            message_id="unregistered",
            route_name="line",
            door="channel",
            thread_id="bridge:line:+15550002222",
            client_address="+15550002222",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask hello",
            answer_status="answered",
            answer="hello",
            created_at=now,
            updated_at=now,
        )
    )

    with pytest.raises(RuntimeError, match="is not registered on this deployment"):
        await delivery_module.deliver("unregistered")
    got = await _store().get_record("unregistered")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.FAILED
    assert got.attempts == 1


# -- the signed callback verifies under the row's secret ----------------------


async def test_api_callback_signature_verifies_under_the_row_secret(env, monkeypatch):
    import hashlib
    import hmac

    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    captured: list[tuple[bytes, str]] = []

    async def _post(url, body, signature, timeout_seconds):
        captured.append((body, signature))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    await turn_module.submit_api_message("chat", "user-7", "hello", "alice", wait_seconds=0)
    await _settle()

    assert len(captured) == 1
    body, signature = captured[0]
    # A receiver recomputes HMAC-SHA256(callback_secret, raw_body) and compares — the
    # signature the executor sent verifies under the route's secret and nothing else.
    expected = "sha256=" + hmac.new(b"sec-1", body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    forged = "sha256=" + hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(signature, forged)


# -- intake ordering: nothing is written until the message is committed -------


class BlockingAgent(Agent):
    """An agent that holds its turn open until released, so a second message for the same
    thread meets a genuinely full per-thread FIFO."""

    tool_name = "block"
    ToolInput = _EchoInput

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, *, user_message: str = "", thread_id: str | None = None, **kwargs):
        self.calls.append(user_message)
        self.entered.set()
        await self.release.wait()
        return f"echo: {user_message}"


def _intake_record(message_id: str, provider_message_id: str):
    """An ``accepted`` record with no outcome — what a worker leaves behind when it stops
    between accepting a message and finishing its turn."""
    from tai42_skeleton.conversations.models import ConversationRecord

    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id=provider_message_id,
        origin="client",
        inbound_text=f"ask {message_id}",
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )


async def _create_stranded_intake(store: ConversationRecordStore, fake: FakeRecordRedis, message_id: str) -> None:
    """Persist an intake record whose intake lease has LAPSED — what a worker that died
    mid-turn leaves behind, and the only shape the re-drive may adopt."""
    await store.create_record(_intake_record(message_id, "PID1"), intake_token="dead-worker")
    key = ConversationsSettings().record_key(message_id)
    fields = await fake.hgetall(key)
    fake.seed_hash(key, fields | {"intake_claim": f"dead-worker:{time.time() - 1}"})


async def _intake_lease(fake: FakeRecordRedis, message_id: str) -> str:
    return (await fake.hgetall(ConversationsSettings().record_key(message_id)))["intake_claim"]


async def _all_record_ids(store: ConversationRecordStore) -> list[str]:
    return sorted(r.message_id for r in await store.list_by_status(frozenset(DeliveryStatus)))


async def test_thread_overflow_writes_no_state_so_the_provider_retry_succeeds(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_THREAD_QUEUE_DEPTH", "1")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)

    # The thread's only slot is taken, so the next message is refused LOUDLY...
    with pytest.raises(caps_module.ThreadQueueOverflowError):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    # ...with ZERO state written: the dedupe pair is unclaimed, so the refusal is
    # honestly retriable.
    assert await store.get_inbound_owner("twilio", "PID2") is None
    assert await _all_record_ids(store) == [first]

    agent.release.set()
    await _settle()

    # The retry after the thread drains is a genuinely fresh attempt, not a dedupe hit
    # on a message that never ran.
    retry = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()

    assert retry != first
    assert agent.calls == ["one", "two"]
    record = await store.get_record(retry)
    assert record is not None
    assert record.answer == "echo: two"
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_a_failed_persist_leaves_the_inbound_pair_unclaimed(env, monkeypatch):
    # The record is persisted BEFORE the claim on every accept path, so a store failure
    # cannot burn the 48h idempotency slot on a message with nothing behind it.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    async def _boom(self, record, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(records_module.ConversationRecordStore, "create_record", _boom)
    store = _store()

    # One accept per admission verdict: admitted, shed with a paid reply, shed silently.
    for provider_message_id in ("PID1", "PID2", "PID3"):
        with pytest.raises(RuntimeError, match="redis is down"):
            await turn_module.accept(
                "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", provider_message_id
            )
        assert await store.get_inbound_owner("twilio", provider_message_id) is None
    # The admitted attempt also gave its FIFO reservation back rather than leaking it.
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_an_indeterminate_inbound_claim_is_resolved_not_left_at_intake(env, monkeypatch):
    # The commit point: the claim's EVAL is APPLIED and only its reply is lost. The pair is
    # now committed to this record, so the provider's redelivery dedupes to it forever —
    # the accept must resolve it before it re-raises, never leave it stranded at intake.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    store = _store()
    claim_inbound = records_module.ConversationRecordStore.claim_inbound

    lost: list[str] = []

    async def _claim_then_lose_the_reply(self, channel_name, provider_message_id, message_id):
        owner = await claim_inbound(self, channel_name, provider_message_id, message_id)
        if not lost:
            # Only the commit-point call loses its reply; the resolution's own arbitration
            # reaches the same claim and reads back what landed.
            lost.append(message_id)
            raise TimeoutError("the reply never came back")
        return owner

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_inbound", _claim_then_lose_the_reply)

    with pytest.raises(TimeoutError):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID-lost")
    await _settle()

    owner = await store.get_inbound_owner("twilio", "PID-lost")
    assert owner is not None
    record = await store.get_record(owner)
    assert record is not None
    # Resolved with the client-safe error outcome and delivered, not left ``accepted``.
    assert record.delivery_status is not DeliveryStatus.ACCEPTED
    assert record.answer_status == "error"
    assert [send.message for send in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]
    # And the FIFO slot the accept reserved was given back.
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_a_racing_duplicate_accept_leaves_one_record_and_releases_its_slot(env, monkeypatch):
    # Two accepts of the SAME provider message id that both got past the redelivery
    # fast-path read: the atomic claim arbitrates them, and the loser owns nothing.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    caps = caps_module.get_turn_caps()
    thread_id = "bridge:line:+15550002222"

    async def _attempt(message_id: str) -> str:
        return await turn_module._accept_for_turn(
            store,
            route=_channel_route(),
            channel="twilio",
            message_id=message_id,
            thread_id=thread_id,
            client_address="+15550002222",
            text="hi",
            provider_message_id="PID1",
        )

    winner = await _attempt("winner")
    loser = await _attempt("loser")

    assert winner == "winner"
    assert loser == "winner"  # the loser answers with the id the pair is committed to
    assert await _all_record_ids(store) == ["winner"]
    # Only the winner's reservation stands; the loser handed its slot straight back.
    assert caps._thread_waiters[thread_id] == 1

    await _settle()

    assert thread_id not in caps._thread_waiters
    assert len(agent.calls) == 1


async def test_a_settings_reload_mid_accept_does_not_leak_the_reserved_slot(env, monkeypatch):
    # A reload rebuilds the caps, and the turn must keep running against the instance its
    # slot was reserved on: released on any other one, the reservation would sit on the old
    # instance forever and permanently narrow the thread's FIFO.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    reserving_caps = caps_module.get_turn_caps()

    create_record = records_module.ConversationRecordStore.create_record

    async def _reload_then_create(self, record, **kwargs):
        # The reload lands after the reservation and before the turn is scheduled.
        caps_module._CAPS_CACHE.clear()
        await create_record(self, record, **kwargs)

    monkeypatch.setattr(records_module.ConversationRecordStore, "create_record", _reload_then_create)

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert agent.calls == [("hi", "bridge:line:+15550002222")]
    assert reserving_caps is not caps_module.get_turn_caps()
    assert reserving_caps._thread_waiters == {}


async def test_redrive_claims_and_adopts_a_record_stranded_before_its_claim(env, monkeypatch):
    # Crash between persisting the intake record and claiming the pair: nobody owns the
    # pair, so the re-drive claims it on the record's behalf and adopts the record.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")

    await turn_module.redrive_accepted()
    await _settle()

    assert await store.get_inbound_owner("twilio", "PID1") == "stranded"
    record = await store.get_record("stranded")
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends  # the error outcome reached the client
    # The turn is NOT re-run: it dispatches authorized tools under a real execution key,
    # so re-running it could repeat a real-world side effect.
    assert agent.calls == []


async def test_redrive_of_a_record_stranded_mid_turn_fails_it_and_never_reruns_the_turn(env, monkeypatch):
    # Crash mid-turn: the intake lease has lapsed and the record already owns its claim, so
    # the re-drive adopts it and terminally fails it rather than running the agent again.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    await turn_module.redrive_accepted()
    await _settle()

    record = await store.get_record("stranded")
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]
    assert agent.calls == []


async def test_a_boot_redrive_leaves_a_live_peers_in_flight_turn_alone(env, monkeypatch):
    # The supported multi-worker shape: a sibling worker boots while this one is mid-turn.
    # The record's intake lease is LIVE, so the re-drive must not terminally fail a message
    # that is being answered correctly.
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)

    await turn_module.redrive_accepted()
    await asyncio.sleep(0)

    # Still at intake, and nothing was sent: the live turn was left to the worker running it.
    mid_turn = await store.get_record(message_id)
    assert mid_turn is not None
    assert mid_turn.delivery_status is DeliveryStatus.ACCEPTED
    assert channel.sends == []

    agent.release.set()
    await _settle()

    # ...and the real answer is the one the client gets.
    record = await store.get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hi"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == ["echo: hi"]


async def test_a_running_turn_refreshes_its_intake_lease_past_the_original_expiry(env, monkeypatch):
    # A turn longer than one lease stays live only because it heartbeats: without the
    # refresh its lease lapses and the next boot reaps it mid-flight.
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS", "1")
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    at_accept = await _intake_lease(env, message_id)

    # Past the lease the accept wrote, so only a refresh can still be holding it.
    await asyncio.sleep(2.3)
    assert await _intake_lease(env, message_id) != at_accept

    await turn_module.redrive_accepted()
    await asyncio.sleep(0)
    mid_turn = await store.get_record(message_id)
    assert mid_turn is not None
    assert mid_turn.delivery_status is DeliveryStatus.ACCEPTED
    assert channel.sends == []

    agent.release.set()
    await _settle()
    record = await store.get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hi"
    # The lease is released with the outcome, not left behind on the record.
    assert await _intake_lease(env, message_id) == ""


async def test_a_turn_queued_behind_the_caps_keeps_its_intake_lease_live(env, monkeypatch):
    # A turn waiting on the per-thread FIFO or the global ceiling has not started yet but is
    # every bit as owned as a running one. Its lease must heartbeat while it waits, or a
    # busy worker's own backlog is reaped out from under it and answered with an error.
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS", "1")
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    running = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    # Same address, so the second message queues behind the first on the thread's FIFO.
    queued = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    assert agent.calls == ["one"]

    # Past the lease the accept wrote: only a heartbeat can still be holding it.
    await asyncio.sleep(2.3)
    await turn_module.redrive_accepted()
    await asyncio.sleep(0)

    for message_id in (running, queued):
        record = await store.get_record(message_id)
        assert record is not None
        assert record.delivery_status is DeliveryStatus.ACCEPTED
    assert channel.sends == []

    agent.release.set()
    await _settle(timeout=5.0)

    assert agent.calls == ["one", "two"]
    assert sorted(n.message for n in channel.sends) == ["echo: one", "echo: two"]


async def test_two_re_drives_adopting_one_stranded_record_still_produce_one_outcome(env, monkeypatch):
    # The lease gate is not the exactly-once guard. With both workers adopting the same
    # record, the guarded complete_turn transition still admits ONE outcome and the client
    # is sent ONE message.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    async def _both_adopt(self, message_id, now, token, lease_seconds):
        return 1

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_intake", _both_adopt)

    await asyncio.gather(turn_module.redrive_accepted(), turn_module.redrive_accepted())
    await _settle()

    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]
    resolved = await store.get_record("stranded")
    assert resolved is not None
    assert resolved.delivery_status is DeliveryStatus.DELIVERED


async def test_a_turn_task_that_dies_resolves_its_own_record_without_waiting_for_a_boot(env, monkeypatch):
    # The worker is still alive and has just watched its own turn task fail, so it knows
    # the turn is over: the record is given its error outcome and delivered right there,
    # not held at intake until whenever the process next restarts.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    async def _die(*, route, intake, text):
        raise RuntimeError("the record store went away mid-turn")

    monkeypatch.setattr(turn_module, "_complete_turn", _die)
    store = _store()

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await store.get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]
    # The thread's FIFO slot is given back too, so the failure costs the thread nothing.
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_redrive_discards_an_intake_record_that_lost_its_claim(env, monkeypatch):
    # The pair is owned by another attempt, so this record is a stranded loser: it is
    # discarded, and nothing is delivered for it.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    store = _store()
    await _create_stranded_intake(store, env, "loser")
    assert await store.claim_inbound("twilio", "PID1", "winner") == "winner"

    await turn_module.redrive_accepted()
    await _settle()

    assert await store.get_record("loser") is None
    assert channel.sends == []


async def test_redrive_isolates_a_failing_record_and_resolves_the_rest(env, monkeypatch):
    # One record raising must not abandon every other stranded record in the pass, nor abort
    # the boot handler that runs it: the failing record is left for the next pass.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    async def _strand(message_id: str, provider_message_id: str) -> None:
        await store.create_record(_intake_record(message_id, provider_message_id), intake_token="dead-worker")
        key = ConversationsSettings().record_key(message_id)
        env.seed_hash(key, (await env.hgetall(key)) | {"intake_claim": f"dead-worker:{time.time() - 1}"})

    # list_by_status orders by member, so "bad" is reached before "good".
    await _strand("bad", "PID1")
    await _strand("good", "PID2")

    real_claim = records_module.ConversationRecordStore.claim_intake

    async def _claim(self, message_id, *args, **kwargs):
        if message_id == "bad":
            raise RuntimeError("a redis blip on this one record")
        return await real_claim(self, message_id, *args, **kwargs)

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_intake", _claim)

    await turn_module.redrive_accepted()
    await _settle()

    left = await store.get_record("bad")
    assert left is not None
    assert left.delivery_status is DeliveryStatus.ACCEPTED
    resolved = await store.get_record("good")
    assert resolved is not None
    assert resolved.answer_status == "error"
    assert resolved.delivery_status is DeliveryStatus.DELIVERED
    assert agent.calls == []


async def test_rate_shed_records_the_paid_reply_and_the_silent_drop(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    admitted = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await _settle()
    replied = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()
    dropped = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "three", "PID3")
    await _settle()

    # Only the admitted message bought a turn.
    assert agent.calls == [("one", "bridge:line:+15550002222")]
    assert await store.get_inbound_owner("twilio", "PID1") == admitted

    # The paid slow-down reply is a real record, persisted before its claim and delivered.
    reply = await store.get_record(replied)
    assert reply is not None
    assert reply.answer == turn_module._SLOW_DOWN_TEXT
    assert reply.delivery_status is DeliveryStatus.DELIVERED
    assert await store.get_inbound_owner("twilio", "PID2") == replied

    # The silent drop sends nothing but still leaves a terminal, auditable record behind
    # its claim — every claim points at an outcome somebody can look up.
    shed = await store.get_record(dropped)
    assert shed is not None
    assert shed.delivery_status is DeliveryStatus.SHED
    assert shed.answer is None
    assert shed.error is not None
    assert await store.get_inbound_owner("twilio", "PID3") == dropped
    assert [n.message for n in channel.sends] == ["echo: one", turn_module._SLOW_DOWN_TEXT]


# -- a blank provider message id is no idempotency key at all -----------------


async def test_a_blank_provider_message_id_is_refused_with_nothing_written(env, monkeypatch):
    # A blank id is not a usable idempotency key: every blank id on the channel names the
    # SAME dedupe marker, so the first such message would swallow every later one as its
    # own redelivery. The door refuses loudly before it writes anything.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})

    for blank in ("", "   "):
        with pytest.raises(ValueError, match="provider_message_id must be a non-blank string"):
            await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", blank)
    await _settle()

    assert agent.calls == []
    assert channel.sends == []
    assert await _store().list_by_status(frozenset(DeliveryStatus)) == []


async def test_a_shed_reply_is_not_deliverable_until_it_owns_its_claim(env, monkeypatch):
    # The slow-down reply is persisted BEFORE the inbound claim, so its pre-claim state
    # must be one the delivery machine never drives; otherwise a sweep pass landing in that
    # window sends a reply for a message another attempt owns.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await _settle()

    async def _claim_lost_after_a_sweep(self, channel_name, provider_message_id, message_id):
        await delivery_module.sweep_stalled_deliveries()
        await _settle()
        return first

    monkeypatch.setattr(ConversationRecordStore, "claim_inbound", _claim_lost_after_a_sweep)
    owner = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()

    assert owner == first
    assert [n.message for n in channel.sends] == ["echo: one"]
    assert await store.list_by_status(frozenset({DeliveryStatus.PENDING_DELIVERY})) == []
