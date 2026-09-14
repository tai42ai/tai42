"""The tool-turn payload, entry params, ordered multi-message parts, and the surfaced ``turn`` block."""

from __future__ import annotations

import logging

import pytest
from tai42_contract.conversations import (
    ConversationRoute,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import record as record_module
from tai42_skeleton.conversations.turn import tool_turn as tool_turn_module
from tai42_skeleton.states.context import current_state_context

from .conftest import (
    _BASELINE_CHANNEL_PAYLOAD,
    FakeChannel,
    FakeManager,
    _accepting_callback,
    _FakeTemplateApp,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


async def test_tool_payload_builder_always_carries_the_thread_id(env, monkeypatch):
    # Unit on the payload builder: the payload the ``payload_expr`` sees always carries this
    # turn's canonical thread_id alongside the message/sender/channel/our_identity fields. An
    # identity expr (".") surfaces the whole payload as the dispatched kwargs.
    route = _tool_channel_route(payload_expr=".")
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(tool_turn_module, "tai42_app", _FakeTemplateApp())
    record = record_module._new_record(
        route=route,
        message_id="m-x",
        thread_id="bridge:tool-line:+15550002222",
        client_address="+15550002222",
        caller_principal=None,
        provider_message_id="PID1",
        inbound_text="hello",
        delivery_status=DeliveryStatus.ACCEPTED,
    )

    await tool_turn_module._run_tool_turn(
        route, "hello", "bridge:tool-line:+15550002222", "+15550002222", record=record
    )

    kwargs = tools.calls[0]["arguments"]
    assert kwargs["thread_id"] == "bridge:tool-line:+15550002222"
    assert kwargs["message"] == "hello"
    assert kwargs["sender"] == "+15550002222"
    assert kwargs["channel"] == "twilio"
    assert kwargs["our_identity"] == "+15550001111"
    # Every tool turn's payload carries the generic turn block, subject included.
    assert kwargs["turn"] == {
        "id": "m-x",
        "inbound": {"id": "PID1", "kind": "message", "source": "twilio"},
        "subject": {
            "target_kind": "tool",
            "target_name": "echo-tool",
            "person": None,
            "thread": "bridge:tool-line:+15550002222",
            "locale": None,
        },
    }


async def test_tool_payload_expr_by_id_renders_then_maps(env, monkeypatch):
    # A by-id payload_expr is rendered through the bound resource manager to its jq program
    # IMMEDIATELY before the map runs, so the tool receives the mapped kwargs.
    route = ConversationRoute(
        route_name="tool-line",
        door="channel",
        target_kind="tool",
        target_name="echo-tool",
        payload_expr=TemplatedText(id="route-payload"),
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
    )
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(tool_turn_module, "tai42_app", _FakeTemplateApp({"route-payload": "{echoed: .message}"}))
    record = record_module._new_record(
        route=route,
        message_id="m-x",
        thread_id="bridge:tool-line:+15550002222",
        client_address="+15550002222",
        caller_principal=None,
        provider_message_id="PID1",
        inbound_text="hello",
        delivery_status=DeliveryStatus.ACCEPTED,
    )

    await tool_turn_module._run_tool_turn(
        route, "hello", "bridge:tool-line:+15550002222", "+15550002222", record=record
    )

    assert tools.calls[0]["arguments"] == {"echoed": "hello"}


class _MediaFakeChannel(FakeChannel):
    """A FakeChannel that ADVERTISES media support, so the delivery executor's capability
    guard lets a media part through and hands it the notification with its media attached."""

    supports_media_notifications = True


async def test_tool_route_array_reply_delivers_ordered_parts(env, monkeypatch):
    # A tool route whose reply_expr emits a JSON array of strings produces an ORDERED
    # multi-message answer: each string is its own part, delivered as its own message in order,
    # and ``answer`` is the blank-line join every legacy reader still sees.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".messages")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"messages": ["first", "second", "third"]})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "first\n\nsecond\n\nthird"
    assert record.answer_parts is not None
    assert [p.message for p in record.answer_parts] == ["first", "second", "third"]
    # Three SEPARATE messages, in order.
    assert [n.message for n in channel.sends] == ["first", "second", "third"]


async def test_tool_route_direct_list_result_mixes_string_and_part_object(env, monkeypatch):
    # A no-reply_expr tool may return a list directly; its elements are EITHER a plain string
    # (text-only part shorthand) or a part object — both normalize to the one internal part.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: ["plain", {"message": "rich object"}])

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_parts is not None
    assert [p.message for p in record.answer_parts] == ["plain", "rich object"]
    assert [n.message for n in channel.sends] == ["plain", "rich object"]


async def test_tool_route_mixed_text_and_media_parts_deliver_in_order(env, monkeypatch):
    # A mixed sequence: text, then a media part, then text — delivered in order, and only the
    # media part's notification carries media.
    channel = _MediaFakeChannel()
    route = _tool_channel_route(reply_expr=".parts")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(
        monkeypatch,
        lambda kw: {
            "parts": [
                "intro text",
                {"message": "the image", "media": [{"kind": "image", "url": "https://cdn.example/i.png"}]},
                "closing text",
            ]
        },
    )

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "intro text\n\nthe image\n\nclosing text"
    assert record.answer_parts is not None
    assert [p.message for p in record.answer_parts] == ["intro text", "the image", "closing text"]
    # Order preserved, and the media rides ONLY the media part's notification.
    assert [n.message for n in channel.sends] == ["intro text", "the image", "closing text"]
    assert channel.sends[0].media is None
    assert channel.sends[1].media is not None
    assert channel.sends[1].media[0].url == "https://cdn.example/i.png"
    assert channel.sends[2].media is None


async def test_tool_route_empty_array_reply_is_a_loud_error(env, monkeypatch):
    # An empty array has no message to send: a loud error outcome, never a silent drop.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".parts")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"parts": []})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "at least one message" in record.error
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_tool_route_blank_array_element_is_a_loud_error(env, monkeypatch, blank):
    # A blank string element would deliver an empty message: a loud error, never coerced away.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".parts")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"parts": ["ok", blank]})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "blank string" in record.error
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_route_malformed_part_object_is_a_loud_error(env, monkeypatch):
    # Parts are strict from birth: an unknown key in a part object is a loud error, never a
    # silently dropped field.
    channel = FakeChannel()
    route = _tool_channel_route(reply_expr=".parts")
    _wire(monkeypatch, FakeManager(route), channel)
    _wire_tool(monkeypatch, lambda kw: {"parts": [{"message": "hi", "bogus": 1}]})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "not a valid part" in record.error
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]


async def test_tool_payload_carries_params_when_passed(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".")  # pass the whole payload through as kwargs
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", {"token": "abc-123"}
    )
    await _settle()

    kwargs = tools.calls[0]["arguments"]
    # The generic turn block rides every tool payload; its id is this turn's record id.
    assert kwargs.pop("turn") == {
        "id": message_id,
        "inbound": {"id": "PID1", "kind": "message", "source": "twilio"},
        "subject": {
            "target_kind": "tool",
            "target_name": "echo-tool",
            "person": None,
            "thread": "bridge:tool-line:+15550002222",
            "locale": None,
        },
    }
    assert kwargs == {**_BASELINE_CHANNEL_PAYLOAD, "params": {"token": "abc-123"}}


@pytest.mark.parametrize("params", [None, {}])
async def test_tool_payload_omits_params_when_none_or_empty(env, monkeypatch, params):
    channel = FakeChannel()
    route = _tool_channel_route(payload_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", params
    )
    await _settle()

    # Byte-identical to today's payload apart from the generic turn block: no ``params`` key.
    kwargs = tools.calls[0]["arguments"]
    assert kwargs.pop("turn") == {
        "id": message_id,
        "inbound": {"id": "PID1", "kind": "message", "source": "twilio"},
        "subject": {
            "target_kind": "tool",
            "target_name": "echo-tool",
            "person": None,
            "thread": "bridge:tool-line:+15550002222",
            "locale": None,
        },
    }
    assert kwargs == _BASELINE_CHANNEL_PAYLOAD
    assert "params" not in kwargs


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


async def test_turn_block_on_the_channel_door(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    kwargs = tools.calls[-1]["arguments"]
    assert kwargs["message"] == "hi"
    assert kwargs["sender"] == "+15550002222"
    assert kwargs["turn"] == {
        "id": message_id,
        "inbound": {"id": "PID1", "kind": "message", "source": "twilio"},
        "subject": {
            "target_kind": "tool",
            "target_name": "echo-tool",
            "person": None,
            "thread": "bridge:tool-line:+15550002222",
            "locale": None,
        },
    }
    assert "event" not in kwargs


async def test_turn_block_on_the_api_door(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    result = await turn_module.submit_api_message("tool-api", "user-7", "hi", "alice", wait_seconds=5)
    await _settle()

    kwargs = tools.calls[-1]["arguments"]
    # An api-door message has no provider id, so the inbound id is the record id, source "api".
    assert kwargs["turn"] == {
        "id": result.message_id,
        "inbound": {"id": result.message_id, "kind": "message", "source": "api"},
        "subject": {
            "target_kind": "tool",
            "target_name": "echo-tool",
            "person": None,
            "thread": result.thread_id,
            "locale": None,
        },
    }
    assert "event" not in kwargs


async def test_operator_send_runs_no_tool_turn_and_carries_no_turn_block(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route()
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.operator_send(
        route=route,
        thread_id="bridge:tool-line:+15550002222",
        client_address="+15550002222",
        text="a human reply",
        operator_principal="op-1",
    )
    await _settle()

    # An operator send mints an already-answered record and runs NO tool turn.
    assert tools.calls == []
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert record.answer == "a human reply"


async def test_conversation_state_context_deposited_for_the_tool_branch(env, monkeypatch):
    # The tool a conversation turn dispatches runs inside a ``conversation``-door state
    # context: candidates carry the thread (a non-multichannel target resolves no person),
    # actor is the turn's attribution user_id, turn_id is the record id.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    seen: list = []
    _wire_tool(monkeypatch, lambda kw: seen.append(current_state_context()) or "ok")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert len(seen) == 1
    ctx = seen[0]
    assert ctx is not None
    assert ctx.door == "conversation"
    assert ctx.candidates.target_kind == "tool"
    assert ctx.candidates.target_name == "echo-tool"
    # Non-multichannel: only the thread candidate, no person.
    assert ctx.candidates.by_kind == {"thread": "bridge:tool-line:+15550002222"}
    assert ctx.turn_id == message_id
    assert ctx.actor is not None
    # The context is torn down once the turn ends. The agent branch shares this exact seam —
    # both branches run inside the single ``with run_attribution(...), state_context(...)`` in
    # ``_target_outcome`` — so the tool-branch capture proves the deposit for both.
    assert current_state_context() is None
