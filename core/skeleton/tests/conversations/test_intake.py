"""The channel door ``accept``: admission, idempotency, shed, and the structured inbound it stamps."""

from __future__ import annotations

import pytest
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import intake as intake_module

from .conftest import (
    _BASELINE_CHANNEL_PAYLOAD,
    EchoAgent,
    FakeChannel,
    FakeManager,
    MemoryAgent,
    _accepting_callback,
    _channel_route,
    _connected,
    _settle,
    _store,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)


async def test_accept_channel_happy_path(env, monkeypatch):
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

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
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    second = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi again", "PID1")
    await _settle()

    assert first == second
    assert len(agent.calls) == 1  # the redelivery started no second turn


async def test_no_matching_route_raises_loudly(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_channel_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    with pytest.raises(turn_module.ConversationRouteResolutionError):
        await turn_module.accept("twilio", "+19999999999", "+15550002222", "+15550002222", "hi", "PID9")


async def test_two_routes_claiming_one_channel_identity_is_a_loud_corrupt_table(env, monkeypatch):
    # Two channel routes bound to the SAME (channel, our_identity) is a corrupt routing table:
    # the resolver refuses to pick one and raises rather than silently routing to either.
    _wire(monkeypatch, FakeManager(_channel_route("line-a"), _channel_route("line-b")))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    with pytest.raises(RuntimeError, match="routing table is inconsistent"):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PIDX")


async def test_blank_inbound_text_is_refused_before_any_state(env, monkeypatch):
    from tai42_contract.conversations import BlankInboundTextError

    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    for blank in ("", "   ", "\t\n"):
        with pytest.raises(BlankInboundTextError):
            await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", blank, "PID1")
    await _settle()

    assert await _store().list_by_status(frozenset(DeliveryStatus)) == []


async def test_a_blank_accountable_cap_key_is_refused_loudly(env, monkeypatch):
    # A door that omits the accountable key must fail at the seam, never fall back to
    # sharing one bucket — a blank cap_key raises rather than resolving.
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="non-blank"):
            await turn_module.accept("twilio", "+15550001111", "+15550002222", blank, "hi", "PID1")
    await _settle()

    assert await _store().list_by_status(frozenset(DeliveryStatus)) == []


async def test_two_messages_from_one_address_remember_via_the_thread_id(env, monkeypatch):
    agent = MemoryAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

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


async def test_rate_shed_records_the_paid_reply_and_the_silent_drop(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
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
    assert reply.answer == intake_module._SLOW_DOWN_TEXT
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
    assert [n.message for n in channel.sends] == ["echo: one", intake_module._SLOW_DOWN_TEXT]


async def test_a_blank_provider_message_id_is_refused_with_nothing_written(env, monkeypatch):
    # A blank id is not a usable idempotency key: every blank id on the channel names the
    # SAME dedupe marker, so the first such message would swallow every later one as its
    # own redelivery. The door refuses loudly before it writes anything.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

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
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
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


_FORM = {"name": "Alice", "size": 42}


async def test_accept_with_form_stamps_the_record_and_the_tool_payload(env, monkeypatch):
    # A channel door hands the turn the participant's structured submission WITH its rendered
    # text: the record stores it beside inbound_text, both read doors publish it, and a
    # tool target's jq payload gains a "form" key the route's start_expr can map.
    channel = FakeChannel()
    route = _tool_channel_route(start_expr="{msg: .message, form: .form}")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice\nsize: 42", "PID1", form=_FORM
    )
    await _settle()

    assert tools.calls[0]["arguments"] == {"msg": "name: Alice\nsize: 42", "form": _FORM}
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_form == _FORM
    assert record.view()["inbound_form"] == _FORM
    assert record.caller_view()["inbound_form"] == _FORM


async def test_accept_without_form_keeps_the_payload_byte_identical(env, monkeypatch):
    # A form-less inbound emits NO "form" key at all — an existing start_expr over the
    # whole payload sees exactly the shape it saw before the field existed.
    channel = FakeChannel()
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert "form" not in tools.calls[0]["arguments"]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_form is None
    assert record.caller_view()["inbound_form"] is None


async def test_accept_form_reaches_an_agent_target_as_text_only(env, monkeypatch):
    # An agent target sees the RENDERED TEXT only — the structured submission stays on the
    # record (and on a tool payload), never in the agent's user message.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice", "PID1", form={"name": "Alice"}
    )
    await _settle()

    assert agent.calls == [("name: Alice", "bridge:line:+15550002222")]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_form == {"name": "Alice"}


async def test_accept_refuses_an_invalid_form_before_any_state(env, monkeypatch):
    # The defensive transport-bounds check runs at the seam (the params pattern): a
    # non-object form is refused loudly BEFORE any record or claim is written.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: "ok")

    with pytest.raises(ValueError, match="form must be a JSON object"):
        await turn_module.accept(
            "twilio",
            "+15550001111",
            "+15550002222",
            "+15550002222",
            "hi",
            "PID1",
            form=["nope"],  # type: ignore[arg-type]
        )
    await _settle()

    assert await _store().get_inbound_owner("twilio", "PID1") is None


async def test_shed_records_carry_the_inbound_form(env, monkeypatch):
    # Both shed shapes stamp the form beside the verbatim text, so a rate-capped
    # submission still reads back complete from the record.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: "ok")
    store = _store()

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await _settle()
    shed_with_reply = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice", "PID2", form={"name": "Alice"}
    )
    await _settle()
    shed_silent = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "name: Bob", "PID3", form={"name": "Bob"}
    )
    await _settle()

    replied = await store.get_record(shed_with_reply)
    assert replied is not None
    assert replied.answer == intake_module._SLOW_DOWN_TEXT
    assert replied.inbound_form == {"name": "Alice"}
    silent = await store.get_record(shed_silent)
    assert silent is not None
    assert silent.delivery_status is DeliveryStatus.SHED
    assert silent.inbound_form == {"name": "Bob"}


async def test_api_submit_with_form_stamps_the_record_and_the_tool_payload(env, monkeypatch):
    # The api door twin: ConversationMessage.form threads through submit_api_message with
    # the same record + payload semantics the channel door has.
    route = _tool_api_route(start_expr="{msg: .message, form: .form}")
    _wire(monkeypatch, FakeManager(route))
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    result = await turn_module.submit_api_message(
        "tool-api", "u-7", "name: Alice", "caller", 2, form={"name": "Alice"}, client_connected=_connected
    )
    await _settle()

    assert tools.calls[0]["arguments"] == {"msg": "name: Alice", "form": {"name": "Alice"}}
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.inbound_form == {"name": "Alice"}
    assert record.caller_view()["inbound_form"] == {"name": "Alice"}


_DOC = MediaItem(kind=MediaKind.DOCUMENT, url="https://cdn.example/report.pdf", filename="report.pdf")


_LOCATION = LocationElement(latitude=51.5, longitude=-0.12, name="HQ")


async def test_accept_with_attachments_and_location_stamps_record_and_the_tool_payload(env, monkeypatch):
    # A channel door hands the turn the participant's structured media and shared location WITH the
    # rendered text: the record stores them beside inbound_text, both read doors publish them,
    # and a tool target's payload gains stable "attachments"/"location" keys.
    channel = FakeChannel()
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept(
        "twilio",
        "+15550001111",
        "+15550002222",
        "+15550002222",
        "see attached",
        "PID1",
        attachments=[_DOC],
        location=_LOCATION,
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
    assert kwargs == {
        **_BASELINE_CHANNEL_PAYLOAD,
        "message": "see attached",
        "attachments": [_DOC.model_dump(mode="json")],
        "location": _LOCATION.model_dump(mode="json"),
    }
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_attachments == [_DOC]
    assert record.inbound_location == _LOCATION
    assert record.caller_view()["inbound_attachments"] == [_DOC.model_dump(mode="json")]
    assert record.caller_view()["inbound_location"] == _LOCATION.model_dump(mode="json")


async def test_accept_without_attachments_or_location_keeps_payload_byte_identical(env, monkeypatch):
    # A plain inbound emits NEITHER key — an existing start_expr over the whole payload sees
    # exactly the shape it saw before the fields existed.
    channel = FakeChannel()
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert "attachments" not in tools.calls[0]["arguments"]
    assert "location" not in tools.calls[0]["arguments"]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_attachments is None
    assert record.inbound_location is None


async def test_accept_attachments_reach_an_agent_target_as_text_only(env, monkeypatch):
    # An agent target sees the RENDERED TEXT only — the structured media/location stay on the
    # record (and a tool payload), never in the agent's user message.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    message_id = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "see attached", "PID1", attachments=[_DOC]
    )
    await _settle()

    assert agent.calls == [("see attached", "bridge:line:+15550002222")]
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.inbound_attachments == [_DOC]


async def test_accept_refuses_invalid_attachments_before_any_state(env, monkeypatch):
    # The defensive list-level media check runs at the seam (the params pattern): a present-but-
    # empty attachments list is refused loudly BEFORE any record or claim is written.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: "ok")

    with pytest.raises(ValueError, match="non-empty list when present"):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", attachments=[])
    await _settle()

    assert await _store().get_inbound_owner("twilio", "PID1") is None


async def test_shed_records_carry_attachments_and_location(env, monkeypatch):
    # Both shed shapes stamp the structured media/location beside the verbatim text.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    _wire_tool(monkeypatch, lambda kw: "ok")
    store = _store()

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await _settle()
    shed_with_reply = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2", attachments=[_DOC]
    )
    await _settle()
    shed_silent = await turn_module.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "three", "PID3", location=_LOCATION
    )
    await _settle()

    replied = await store.get_record(shed_with_reply)
    assert replied is not None
    assert replied.inbound_attachments == [_DOC]
    silent = await store.get_record(shed_silent)
    assert silent is not None
    assert silent.delivery_status is DeliveryStatus.SHED
    assert silent.inbound_location == _LOCATION


async def test_api_submit_with_attachments_and_location(env, monkeypatch):
    # The api door twin: ConversationMessage.attachments/.location thread through
    # submit_api_message with the same record + payload semantics the channel door has.
    route = _tool_api_route(start_expr="{msg: .message, attachments: .attachments, location: .location}")
    _wire(monkeypatch, FakeManager(route))
    tools = _wire_tool(monkeypatch, lambda kw: "ok")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    result = await turn_module.submit_api_message(
        "tool-api",
        "u-7",
        "see attached",
        "caller",
        2,
        attachments=[_DOC],
        location=_LOCATION,
        client_connected=_connected,
    )
    await _settle()

    assert tools.calls[0]["arguments"] == {
        "msg": "see attached",
        "attachments": [_DOC.model_dump(mode="json")],
        "location": _LOCATION.model_dump(mode="json"),
    }
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.inbound_attachments == [_DOC]
    assert record.inbound_location == _LOCATION
