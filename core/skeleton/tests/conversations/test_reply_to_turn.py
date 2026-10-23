"""The quoted message on a turn: ``accept`` and ``submit_api_message`` resolve it onto the record's
``inbound_reply_to`` and a tool target's payload carries it under ``reply_to``.
"""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.conversations import ChannelConversationRoute, OverlapPolicy, ReplyTo, ReplyToStatus
from tai42_contract.template import TemplatedText

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import intake as intake_module

from .conftest import (
    FakeChannel,
    FakeManager,
    _accepting_callback,
    _connected,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)

_OUR = "+15550001111"
_ADDR = "+15550002222"


@pytest.fixture(autouse=True)
def _seed_routes(env):
    """Plant the routing rows the record create checks before writing a thread's indexes."""
    for name in ("tool-line", "tool-api"):
        env.seed_route(name)


async def _accept(text: str, pid: str, *, medium: str | None, quotes: str | None, **extra) -> str:
    return await turn_module.accept(
        "twilio",
        _OUR,
        _ADDR,
        _ADDR,
        text,
        pid,
        medium_message_id=medium,
        reply_to_medium_message_id=quotes,
        **extra,
    )


async def test_a_reply_quoting_an_earlier_message_carries_its_text(env, monkeypatch):
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await _accept("the earlier message", "PID1", medium="M-1", quotes=None)
    await _settle()
    message_id = await _accept("about that", "PID2", medium="M-2", quotes="M-1")
    await _settle()

    quote = ReplyTo(message_id="M-1", status=ReplyToStatus.FOUND, text="the earlier message")
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_reply_to == quote
    assert record.caller_view()["inbound_reply_to"] == quote.model_dump(mode="json")
    assert tools.calls[1]["arguments"]["reply_to"] == quote.model_dump(mode="json")
    assert "reply_to" not in tools.calls[0]["arguments"]


async def test_a_reply_quoting_an_unknown_message_is_not_found(env, monkeypatch):
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await _accept("about that", "PID1", medium="M-2", quotes="M-unknown")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_reply_to == ReplyTo(message_id="M-unknown", status=ReplyToStatus.NOT_FOUND, text=None)
    assert tools.calls[0]["arguments"]["reply_to"] == {"message_id": "M-unknown", "status": "not_found", "text": None}


async def test_a_message_quoting_nothing_has_no_quote_and_no_payload_key(env, monkeypatch):
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    async def _never(*args, **kwargs):
        raise AssertionError("a message quoting nothing resolves nothing")

    monkeypatch.setattr(intake_module, "resolve_reply_to", _never)
    message_id = await _accept("hi", "PID1", medium="M-1", quotes=None)
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_reply_to is None
    assert "reply_to" not in tools.calls[0]["arguments"]


async def test_a_redelivery_resolves_nothing_and_returns_the_first_id(env, monkeypatch):
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: "ok")

    first = await _accept("about that", "PID1", medium="M-2", quotes="M-1")
    await _settle()

    async def _never(*args, **kwargs):
        raise AssertionError("a redelivery resolves nothing")

    monkeypatch.setattr(intake_module, "resolve_reply_to", _never)
    assert await _accept("about that", "PID1", medium="M-2", quotes="M-1") == first


async def test_a_store_error_while_resolving_propagates_and_claims_nothing(env, monkeypatch):
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: "ok")

    async def _broken(*args, **kwargs):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(intake_module, "resolve_reply_to", _broken)
    with pytest.raises(ConnectionError, match="store unreachable"):
        await _accept("about that", "PID1", medium="M-2", quotes="M-1")

    assert await _store().get_inbound_owner("twilio", "PID1") is None


async def test_shed_records_carry_the_quote(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), FakeChannel())
    _wire_tool(monkeypatch, lambda kw: "ok")
    store = _store()

    await _accept("the earlier message", "PID1", medium="M-1", quotes=None)
    await _settle()
    shed_with_reply = await _accept("two", "PID2", medium="M-2", quotes="M-1")
    await _settle()
    shed_silent = await _accept("three", "PID3", medium="M-3", quotes="M-1")
    await _settle()

    quote = ReplyTo(message_id="M-1", status=ReplyToStatus.FOUND, text="the earlier message")
    replied = await store.get_record(shed_with_reply)
    assert replied is not None
    assert replied.inbound_reply_to == quote
    silent = await store.get_record(shed_silent)
    assert silent is not None
    assert silent.delivery_status is DeliveryStatus.SHED
    assert silent.inbound_reply_to == quote


async def test_a_batched_turn_carries_each_message_quote_in_its_entry(env, monkeypatch):
    route = ChannelConversationRoute(
        route_name="tool-line",
        door="channel",
        target_kind="tool",
        target_name="echo-tool",
        start_expr=TemplatedText(content="."),
        execution_key="svc",
        channel="twilio",
        our_identity=_OUR,
        execution_key_fingerprint="fp-1",
        overlap=OverlapPolicy(deliver="all", settle_seconds=1),
    )
    _wire(monkeypatch, FakeManager(route), FakeChannel())
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await _accept("the earlier message", "PID1", medium="M-1", quotes=None)
    await _settle(timeout=6.0)
    await _accept("one", "PID2", medium="M-2", quotes="M-1")
    await asyncio.sleep(0.2)  # inside the lead's settle window
    await _accept("two", "PID3", medium="M-3", quotes=None)
    await _settle(timeout=6.0)

    entries = tools.calls[1]["arguments"]["messages"]
    assert [entry["text"] for entry in entries] == ["one", "two"]
    assert entries[0]["reply_to"] == {"message_id": "M-1", "status": "found", "text": "the earlier message"}
    assert "reply_to" not in entries[1]


async def test_an_api_message_quoting_an_earlier_answer_carries_its_text(env, monkeypatch):
    route = _tool_api_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route))
    tools = _wire_tool(monkeypatch, lambda kw: "the earlier answer")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    first = await turn_module.submit_api_message("tool-api", "u-7", "hi", "caller", 2, client_connected=_connected)
    await _settle()
    second = await turn_module.submit_api_message(
        "tool-api", "u-7", "about that", "caller", 2, reply_to_message_id=first.message_id, client_connected=_connected
    )
    await _settle()

    quote = ReplyTo(message_id=first.message_id, status=ReplyToStatus.FOUND, text="the earlier answer")
    record = await _store().get_record(second.message_id)
    assert record is not None
    assert record.inbound_reply_to == quote
    assert tools.calls[1]["arguments"]["reply_to"] == quote.model_dump(mode="json")


async def test_an_api_message_quoting_another_threads_record_is_not_found(env, monkeypatch):
    route = _tool_api_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route))
    _wire_tool(monkeypatch, lambda kw: "the earlier answer")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    first = await turn_module.submit_api_message("tool-api", "u-7", "hi", "caller", 2, client_connected=_connected)
    await _settle()
    other = await turn_module.submit_api_message(
        "tool-api", "u-8", "about that", "caller", 2, reply_to_message_id=first.message_id, client_connected=_connected
    )
    await _settle()

    record = await _store().get_record(other.message_id)
    assert record is not None
    assert record.inbound_reply_to == ReplyTo(message_id=first.message_id, status=ReplyToStatus.NOT_FOUND, text=None)
