"""The ``turn.reply_window`` block: read once per turn from the channel, fresh again on a late reply."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel
from tai42_contract.agent.events import MessageFinal
from tai42_contract.channels import ReplyWindow
from tai42_contract.conversations import ChannelConversationRoute, ConversationEvent, ConversationEventSubmission
from tai42_contract.interactions import PARK_COMPLETION_SUCCEEDED
from tai42_contract.template import TemplatedText
from tests.agent._memoryless import MemorylessTestAgent

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import record as record_module
from tai42_skeleton.conversations.turn import reply_window as reply_window_module
from tai42_skeleton.conversations.turn.reply_window import read_turn_reply_window
from tai42_skeleton.runs.chokepoint import delivery_fire

from .conftest import (
    _TURN_LOGGER,
    FakeChannel,
    FakeManager,
    _accepting_callback,
    _connected,
    _FakeApp,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
    create_record,
)

CLOSES_AT = datetime(2026, 10, 23, 9, 30, tzinfo=UTC)
OPEN = ReplyWindow(state="open", closes_at=CLOSES_AT)
OPEN_JSON = {"state": "open", "closes_at": "2026-10-23T09:30:00Z"}
CLOSED = ReplyWindow(state="closed", closes_at=None)
NONE_JSON = {"state": "none", "closes_at": None}
OUR_IDENTITY = "+15550001111"
CLIENT = "+15550002222"


class WindowChannel(FakeChannel):
    """A channel that reports a reply window: the value it holds, or the exception it holds raised."""

    def __init__(self, window: object) -> None:
        super().__init__()
        self.window = window
        self.reads: list[tuple[str, str | None]] = []

    async def reply_window(self, *, recipient: str, sender_identity: str | None = None) -> object:
        self.reads.append((recipient, sender_identity))
        if isinstance(self.window, Exception):
            raise self.window
        return self.window


class _WindowInput(BaseModel):
    user_message: str | dict[str, Any] = ""
    window: dict[str, Any] | None = None


class WindowAgent(MemorylessTestAgent):
    """An agent whose ``astream`` records the kwargs its route's ``start_expr`` built."""

    tool_name = "echo"
    ToolInput = _WindowInput

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def run(self, **kwargs):  # pragma: no cover - astream is overridden
        raise NotImplementedError

    async def astream(self, **kwargs):
        self.seen.append(kwargs)
        yield MessageFinal(text="done")


class _NoChannels:
    def get(self, name: str) -> FakeChannel:
        raise KeyError(f"unknown channel {name!r}")


class _NoChannelsApp:
    channels = _NoChannels()


def _reply_window_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _TURN_LOGGER and r.levelno == logging.ERROR and "reply window" in r.getMessage()
    ]


# --- The read helper ------------------------------------------------------------


async def test_no_channel_reads_none():
    assert await read_turn_reply_window(None, None, CLIENT) == ReplyWindow(state="none", closes_at=None)


async def test_no_client_address_reads_none(monkeypatch):
    channel = WindowChannel(OPEN)
    monkeypatch.setattr(reply_window_module, "tai42_app", _FakeApp(channel))

    assert await read_turn_reply_window("twilio", OUR_IDENTITY, None) == ReplyWindow(state="none", closes_at=None)
    assert channel.reads == []


async def test_a_channel_without_the_member_reads_none(monkeypatch):
    monkeypatch.setattr(reply_window_module, "tai42_app", _FakeApp(FakeChannel()))

    assert await read_turn_reply_window("twilio", OUR_IDENTITY, CLIENT) == ReplyWindow(state="none", closes_at=None)


async def test_the_member_is_asked_for_the_pair(monkeypatch):
    channel = WindowChannel(OPEN)
    monkeypatch.setattr(reply_window_module, "tai42_app", _FakeApp(channel))

    assert await read_turn_reply_window("twilio", OUR_IDENTITY, CLIENT) == OPEN
    assert channel.reads == [(CLIENT, OUR_IDENTITY)]


async def test_an_unregistered_channel_raises(monkeypatch):
    monkeypatch.setattr(reply_window_module, "tai42_app", _NoChannelsApp())

    with pytest.raises(KeyError, match="twilio"):
        await read_turn_reply_window("twilio", OUR_IDENTITY, CLIENT)


@pytest.mark.parametrize("returned", [None, {"state": "open"}, "open"])
async def test_a_member_returning_something_else_raises(monkeypatch, returned):
    monkeypatch.setattr(reply_window_module, "tai42_app", _FakeApp(WindowChannel(returned)))

    with pytest.raises(TypeError, match="ReplyWindow"):
        await read_turn_reply_window("twilio", OUR_IDENTITY, CLIENT)


# --- On the live turn -------------------------------------------------------------


async def test_a_tool_turn_carries_the_channel_window(env, monkeypatch):
    channel = WindowChannel(OPEN)
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept(
        "twilio", OUR_IDENTITY, CLIENT, CLIENT, "hi", "PID1", medium_message_id=None, reply_to_medium_message_id=None
    )
    await _settle()

    assert tools.calls[0]["arguments"]["turn"]["reply_window"] == OPEN_JSON
    assert channel.reads == [(CLIENT, OUR_IDENTITY)]


async def test_a_tool_turn_on_a_channel_without_the_member_carries_none(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_channel_route()), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.accept(
        "twilio", OUR_IDENTITY, CLIENT, CLIENT, "hi", "PID1", medium_message_id=None, reply_to_medium_message_id=None
    )
    await _settle()

    assert tools.calls[0]["arguments"]["turn"]["reply_window"] == NONE_JSON


async def test_an_api_door_turn_carries_none(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    monkeypatch.setattr(reply_window_module, "tai42_app", _NoChannelsApp())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message(
        "tool-api", "user-7", "hi", "alice", wait_seconds=5, client_connected=_connected
    )
    await _settle()

    assert tools.calls[0]["arguments"]["turn"]["reply_window"] == NONE_JSON


async def test_an_agent_turn_reads_the_window_through_start_expr(env, monkeypatch):
    channel = WindowChannel(OPEN)
    route = ChannelConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity=OUR_IDENTITY,
        execution_key_fingerprint="fp-1",
        start_expr=TemplatedText(content="{user_message: {content: .message}, window: .turn.reply_window}"),
    )
    agent = WindowAgent()
    _wire(monkeypatch, FakeManager(route), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    await turn_module.accept(
        "twilio", OUR_IDENTITY, CLIENT, CLIENT, "hi", "PID1", medium_message_id=None, reply_to_medium_message_id=None
    )
    await _settle()

    assert len(agent.seen) == 1
    assert agent.seen[0]["window"] == OPEN_JSON


async def test_an_event_door_turn_carries_the_channel_window(env, monkeypatch):
    channel = WindowChannel(CLOSED)
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    env.seed_route("tool-line")
    _wire_tool(monkeypatch, lambda kw: "seed")
    await turn_module.accept(
        "twilio", OUR_IDENTITY, CLIENT, CLIENT, "hello", "SEED", medium_message_id=None, reply_to_medium_message_id=None
    )
    await _settle()
    channel.window = OPEN
    tools = _wire_tool(monkeypatch, lambda kw: "handled")

    submission = ConversationEventSubmission(
        address=CLIENT, thread_id=None, event=ConversationEvent(event_id="evt-1", kind="provider.update", payload={})
    )
    await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)
    await _settle()

    assert tools.calls[-1]["arguments"]["turn"]["reply_window"] == OPEN_JSON
    assert channel.reads[-1] == (CLIENT, OUR_IDENTITY)


@pytest.mark.parametrize(
    ("window", "error_class"),
    [(ConnectionError("vendor store down"), "ConnectionError"), ({"state": "open"}, "TypeError")],
)
async def test_a_failed_read_is_a_recorded_error_outcome(env, monkeypatch, caplog, window, error_class):
    channel = WindowChannel(window)
    route = _tool_channel_route()
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "never")

    with caplog.at_level(logging.ERROR, logger=_TURN_LOGGER):
        message_id = await turn_module.accept(
            "twilio",
            OUR_IDENTITY,
            CLIENT,
            CLIENT,
            "hi",
            "PID1",
            medium_message_id=None,
            reply_to_medium_message_id=None,
        )
        await _settle()

    assert tools.calls == []
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error == f"reply window read failed ({error_class})"
    assert record.answer == outcome_module._error_answer_text(route)
    assert [n.message for n in channel.sends] == [outcome_module._error_answer_text(route)]
    errors = _reply_window_errors(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None


# --- On the late reply --------------------------------------------------------------


async def _seed_intake(route) -> None:
    intake = record_module._new_record(
        route=route,
        message_id="intake-1",
        thread_id=f"bridge:{route.route_name}:{CLIENT}",
        client_address=CLIENT,
        caller_principal=None,
        provider_message_id="PID1",
        inbound_text="hi",
        delivery_status=DeliveryStatus.SILENT,
    )
    await create_record(_store(), intake, route=route)


def _agent_reply_route() -> ChannelConversationRoute:
    return ChannelConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity=OUR_IDENTITY,
        execution_key_fingerprint="fp-1",
        reply_expr=TemplatedText(content="$turn.reply_window | tojson"),
    )


async def test_a_late_tool_reply_reads_the_window_fresh(env, monkeypatch):
    channel = WindowChannel(CLOSED)
    route = _tool_channel_route(reply_expr="$turn.reply_window | tojson")
    _wire(monkeypatch, FakeManager(route), channel)
    await _seed_intake(route)
    channel.window = OPEN

    with delivery_fire("c-late"):
        out = await turn_module.deliver_tool_completion(
            thread_id=f"bridge:tool-line:{CLIENT}",
            completion_id="c-late",
            result={"x": 1},
            status=PARK_COMPLETION_SUCCEEDED,
            message_id="intake-1",
        )
    await _settle()

    assert out == {"message_id": "c-late"}
    delivered = await _store().get_record("c-late")
    assert delivered is not None
    assert json.loads(delivered.answer or "") == OPEN_JSON
    assert channel.reads == [(CLIENT, OUR_IDENTITY)]


async def test_a_late_agent_reply_reads_the_window_fresh(env, monkeypatch):
    channel = WindowChannel(CLOSED)
    route = _agent_reply_route()
    _wire(monkeypatch, FakeManager(route), channel)
    await _seed_intake(route)
    channel.window = OPEN

    with delivery_fire("c-late"):
        out = await turn_module.deliver_agent_completion(
            thread_id=f"bridge:line:{CLIENT}",
            completion_id="c-late",
            result={"final": "done"},
            status=PARK_COMPLETION_SUCCEEDED,
            message_id="intake-1",
        )
    await _settle()

    assert out == {"message_id": "c-late"}
    delivered = await _store().get_record("c-late")
    assert delivered is not None
    assert json.loads(delivered.answer or "") == OPEN_JSON
    assert channel.reads == [(CLIENT, OUR_IDENTITY)]


@pytest.mark.parametrize("fire", ["tool", "agent"])
async def test_a_late_read_fault_delivers_the_client_safe_notice(env, monkeypatch, caplog, fire):
    channel = WindowChannel(ConnectionError("vendor store down"))
    if fire == "tool":
        route = _tool_channel_route(reply_expr="$turn.reply_window | tojson")
        deliver = turn_module.deliver_tool_completion
    else:
        route = _agent_reply_route()
        deliver = turn_module.deliver_agent_completion
    _wire(monkeypatch, FakeManager(route), channel)
    await _seed_intake(route)

    with caplog.at_level(logging.ERROR, logger=_TURN_LOGGER), delivery_fire("c-fault"):
        out = await deliver(
            thread_id=f"bridge:{route.route_name}:{CLIENT}",
            completion_id="c-fault",
            result={"x": 1},
            status=PARK_COMPLETION_SUCCEEDED,
            message_id="intake-1",
        )
    await _settle()

    assert out == {"message_id": "c-fault"}
    delivered = await _store().get_record("c-fault")
    assert delivered is not None
    assert delivered.answer == outcome_module._error_answer_text(route)
    assert [n.message for n in channel.sends] == [outcome_module._error_answer_text(route)]
    failures = [r for r in caplog.records if r.name == _TURN_LOGGER and r.levelno == logging.ERROR]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
