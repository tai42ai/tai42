"""The event door ``submit_event``: the turn/event payload, per-door delivery, idempotency, and guards."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import pytest
from fastmcp.tools import Tool
from tai42_contract.app import TaiApp
from tai42_contract.conversations import (
    ConversationEvent,
    ConversationEventSubmission,
    Person,
    PersonAddress,
    TargetConversationConfig,
)

from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.caps import AddressAdmission
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import keys as keys_module
from tai42_skeleton.conversations.turn import pairing as pairing_module
from tai42_skeleton.conversations.turn import target as target_module
from tai42_skeleton.presets.bind import preset_bind

from .conftest import (
    FakeChannel,
    FakeManager,
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
from .fake_record_redis import make_record_client_ctx


def _event(event_id: str = "evt-1", kind: str = "provider.update", payload: dict | None = None) -> ConversationEvent:
    return ConversationEvent(event_id=event_id, kind=kind, payload=payload or {})


def _event_submission(
    *,
    address: str | None = None,
    thread_id: str | None = None,
    event: ConversationEvent | None = None,
    wait_seconds: int = 0,
) -> ConversationEventSubmission:
    return ConversationEventSubmission(
        address=address, thread_id=thread_id, event=event or _event(), wait_seconds=wait_seconds
    )


async def _seed_channel_thread(monkeypatch, fake, channel: FakeChannel) -> str:
    """Open the ``tool-line`` channel thread with one ordinary message turn, returning its
    id. The caller re-wires its own tool afterwards (monkeypatch replaces the seed tool).
    ``fake`` is the record redis: its routing row must exist for the create to index the
    thread (the same row the real conversations manager plants at route create)."""
    fake.seed_route("tool-line")
    _wire_tool(monkeypatch, lambda kw: "seed")
    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hello", "SEED")
    await _settle()
    channel.sends.clear()
    return "bridge:tool-line:+15550002222"


async def test_turn_block_on_the_event_door(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route()
    _wire(monkeypatch, FakeManager(route), channel)
    thread_id = await _seed_channel_thread(monkeypatch, env, channel)

    tools = _wire_tool(monkeypatch, lambda kw: "handled")
    submission = _event_submission(address="+15550002222", event=_event(payload={"amount": 5}))
    result = await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)
    await _settle()

    kwargs = tools.calls[-1]["arguments"]
    # An event turn nulls message/sender and carries the structured event; the turn block
    # names the event id and an ``event:{kind}`` source.
    assert kwargs["message"] is None
    assert kwargs["sender"] is None
    assert kwargs["turn"] == {
        "id": result.message_id,
        "inbound": {"id": "evt-1", "kind": "event", "source": "event:provider.update"},
        "subject": {
            "target_kind": "tool",
            "target_name": "echo-tool",
            "person": None,
            "thread": thread_id,
            "locale": None,
        },
    }
    assert kwargs["event"] == {"id": "evt-1", "kind": "provider.update", "payload": {"amount": 5}}
    assert result.thread_id == thread_id


async def test_event_channel_thread_delivers_the_reply_to_the_address(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    channel.sends.clear()

    _wire_tool(monkeypatch, lambda kw: f"event {kw['event']['kind']} handled")
    submission = _event_submission(address="+15550002222", event=_event())
    result = await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)
    await _settle()

    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.inbound_kind == "event"
    assert record.answer_status == "answered"
    assert record.answer == "event provider.update handled"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    # The reply went to the thread's address, from the route identity.
    assert channel.sends[-1].recipient == "+15550002222"
    assert channel.sends[-1].sender_identity == "+15550001111"


async def test_event_channel_record_carries_no_caller_principal_and_a_submitted_by(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    _wire_tool(monkeypatch, lambda kw: "ok")

    result = await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    await _settle()

    record = await _store().get_record(result.message_id)
    assert record is not None
    # A channel-target event record honours _new_record's door invariant (no caller_principal)
    # while recording its authorizer in the generic ``submitted_by`` provenance field.
    assert record.caller_principal is None
    assert record.submitted_by == "svc-int"


async def test_event_redelivery_returns_the_original_id_and_runs_one_turn(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    submission = _event_submission(address="+15550002222", event=_event(event_id="evt-42"))
    first = await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)
    await _settle()
    second = await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)
    await _settle()

    assert first.message_id == second.message_id
    assert len(tools.calls) == 1  # the redelivery started no second turn


async def test_event_api_thread_posts_a_signed_callback(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    # Open the api thread with an ordinary api message.
    _wire_tool(monkeypatch, lambda kw: "seed")
    await turn_module.submit_api_message("tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected)
    await _settle()

    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature, json.loads(body)))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    _wire_tool(monkeypatch, lambda kw: "event answer")
    result = await turn_module.submit_event(
        "tool-api", _event_submission(address="u-7", event=_event()), "alice", client_connected=_connected
    )
    assert result.answer is None  # 202: the answer is delivered out of band
    await _settle()

    assert len(posted) == 1  # exactly one callback, no double-fire
    url, signature, sent = posted[0]
    assert url == "https://cb.example/x"
    assert signature.startswith("sha256=")
    assert sent == {
        "message_id": result.message_id,
        "thread_id": result.thread_id,
        "status": "answered",
        "answer": "event answer",
    }


async def test_event_api_thread_inline_wait_returns_the_answer_and_suppresses_the_callback(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    _wire_tool(monkeypatch, lambda kw: "seed")
    await turn_module.submit_api_message("tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected)
    await _settle()

    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    _wire_tool(monkeypatch, lambda kw: "inline answer")
    result = await turn_module.submit_event(
        "tool-api",
        _event_submission(address="u-7", event=_event(), wait_seconds=5),
        "alice",
        client_connected=_connected,
    )
    await _settle()

    assert result.answer is not None
    assert result.answer.answer == "inline answer"
    assert posted == []  # the sync wait delivered it; no callback fired


async def test_event_api_thread_gone_client_falls_to_the_callback(env, monkeypatch):
    # The event turn finishes inside the window but the client has hung up (probe False): the
    # inline claim is NOT taken, so no inline answer is returned and the answer is delivered by
    # exactly one signed callback — the same precondition the message door applies.
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    _wire_tool(monkeypatch, lambda kw: "seed")
    await turn_module.submit_api_message("tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected)
    await _settle()

    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    _wire_tool(monkeypatch, lambda kw: "event answer")

    async def _gone() -> bool:
        return False

    result = await turn_module.submit_event(
        "tool-api",
        _event_submission(address="u-7", event=_event(), wait_seconds=5),
        "alice",
        client_connected=_gone,
    )
    await _settle()

    assert result.answer is None  # no inline answer written to a gone client
    assert len(posted) == 1  # delivered by exactly one signed callback
    assert posted[0][1].startswith("sha256=")


async def test_event_channel_thread_never_posts_a_callback(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    _wire_tool(monkeypatch, lambda kw: "ok")
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    await _settle()

    assert posted == []  # a channel-target event delivers to the address, never a callback


async def test_event_unknown_thread_raises_thread_not_found(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_channel_route()))
    _wire_tool(monkeypatch, lambda kw: "ok")
    with pytest.raises(turn_module.ThreadNotFoundError):
        await turn_module.submit_event(
            "tool-line",
            _event_submission(address="+15559998888", event=_event()),
            "svc-int",
            client_connected=_connected,
        )


async def test_event_unknown_thread_id_raises_thread_not_found(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_channel_route()))
    _wire_tool(monkeypatch, lambda kw: "ok")
    with pytest.raises(turn_module.ThreadNotFoundError):
        await turn_module.submit_event(
            "tool-line",
            _event_submission(thread_id="bridge:tool-line:nobody", event=_event()),
            "svc-int",
            client_connected=_connected,
        )


async def test_event_agent_target_route_refuses(env, monkeypatch):
    # An agent target is refused at route lookup, before any thread resolution.
    _wire(monkeypatch, FakeManager(_channel_route()))  # an AGENT route ("line")
    with pytest.raises(turn_module.EventTargetNotToolError):
        await turn_module.submit_event(
            "line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
        )


def _seed_multichannel_linked_person(monkeypatch, fake, *, pid: str = "person-xyz") -> Person:
    """Seed a 2-address LINKED person on the multichannel ``tool-line`` (``tool``/``echo-tool``)
    target and wire the person store to ``fake``. The channel address ``+15550002222`` indexes
    to the person, so any turn on that address aggregates to the ``@person`` thread."""
    monkeypatch.setattr(persons_module, "client_ctx", make_record_client_ctx(fake))
    fake.seed_route("tool-line")  # the routing row a create indexes the thread under
    settings = ConversationsSettings()
    now = datetime.now(UTC)
    channel_addr = PersonAddress(
        door="channel",
        routes=["tool-line"],
        channel="twilio",
        our_identity="+15550001111",
        address="+15550002222",
        linked_at=now,
    )
    api_addr = PersonAddress(
        door="api", routes=["tool-api"], channel=None, our_identity=None, address="alice/u7", linked_at=now
    )
    person = Person(
        person_id=pid, target_kind="tool", target_name="echo-tool", created_at=now, addresses=[channel_addr, api_addr]
    )
    fake._strings[settings.person_key(pid)] = person.model_dump_json()
    dak = json.dumps(["twilio", "+15550001111", "+15550002222"], separators=(",", ":"))
    fake._hashes.setdefault(settings.person_index_key("tool", "echo-tool"), {})[dak] = pid
    config = TargetConversationConfig(target_kind="tool", target_name="echo-tool", multichannel=True)
    fake._strings[settings.target_config_key("tool", "echo-tool")] = config.model_dump_json()
    return person


async def test_event_on_a_linked_person_thread_carries_the_person_fields_like_a_message(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(start_expr=".")  # identity expr surfaces the whole payload
    _wire(monkeypatch, FakeManager(route), channel)
    _seed_multichannel_linked_person(monkeypatch, env)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    # A message turn on the linked address aggregates to the @person thread and carries the
    # person fields in its payload.
    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    message_kwargs = tools.calls[-1]["arguments"]
    assert message_kwargs["thread_id"].startswith(PERSON_THREAD_PREFIX)
    assert message_kwargs["person_id"] == "person-xyz"
    assert len(message_kwargs["person_addresses"]) == 2

    # An event to the SAME thread carries identical person fields.
    result = await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    await _settle()
    event_kwargs = tools.calls[-1]["arguments"]
    assert event_kwargs["thread_id"] == message_kwargs["thread_id"]
    assert event_kwargs["person_id"] == message_kwargs["person_id"]
    assert event_kwargs["person_addresses"] == message_kwargs["person_addresses"]
    assert result.thread_id == message_kwargs["thread_id"]


async def test_event_on_a_linked_person_thread_never_writes_greets_or_classifies(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    _seed_multichannel_linked_person(monkeypatch, env)

    ensure_calls: list = []
    classify_calls: list = []
    greeting_calls: list = []
    original_store = accessors_module._person_store

    class _SpyStore:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def ensure_provisional(self, *args, **kwargs):
            ensure_calls.append((args, kwargs))
            return await self._inner.ensure_provisional(*args, **kwargs)

        async def get_person(self, *args, **kwargs):
            return await self._inner.get_person(*args, **kwargs)

        async def get_by_id(self, *args, **kwargs):
            return await self._inner.get_by_id(*args, **kwargs)

    monkeypatch.setattr(accessors_module, "_person_store", lambda: _SpyStore(original_store()))
    real_classify = target_module.classify
    monkeypatch.setattr(target_module, "classify", lambda text: classify_calls.append(text) or real_classify(text))
    real_greeting = pairing_module._greeting_and_code

    async def _greeting_spy(multichannel):
        greeting_calls.append(multichannel)
        return await real_greeting(multichannel)

    monkeypatch.setattr(pairing_module, "_greeting_and_code", _greeting_spy)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    # A message turn establishes the @person thread AND runs the person-write path.
    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    assert ensure_calls  # the message turn wrote the person side
    assert classify_calls  # and classified the inbound text
    ensure_calls.clear()
    classify_calls.clear()
    greeting_calls.clear()

    # The event turn resolves the SAME person READ-ONLY: no provisional write, no greeting, no
    # classify — a structured event has no human sender to admit.
    await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    await _settle()
    assert ensure_calls == []
    assert classify_calls == []
    assert greeting_calls == []
    assert tools.calls[-1]["arguments"]["person_id"] == "person-xyz"


async def test_event_on_a_plain_thread_carries_no_person_fields(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(start_expr=".")
    _wire(monkeypatch, FakeManager(route), channel)
    # A plain (non-multichannel) thread: no person exists, so neither door carries person fields.
    await _seed_channel_thread(monkeypatch, env, channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    await _settle()
    kwargs = tools.calls[-1]["arguments"]
    assert not kwargs["thread_id"].startswith(PERSON_THREAD_PREFIX)
    assert "person_id" not in kwargs
    assert "person_addresses" not in kwargs


class _OrderedBlockingTools:
    """A tool registry that blocks the FIRST turn until released and records the turn kind of
    each dispatch in the order it is invoked — so the FIFO's serialization is observable."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.first_entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.calls: list[dict] = []

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False, extras=None):
        self.calls.append(arguments)
        kind = arguments["turn"]["inbound"]["kind"]
        self.order.append(kind)
        if kind == "message":
            self.first_entered.set()
            await self.gate.wait()
        return "ok"


async def test_event_runs_after_an_in_flight_message_turn_on_the_thread(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    env.seed_route("tool-line")
    tools = _OrderedBlockingTools()
    monkeypatch.setattr(accessors_module, "_tools", lambda: tools)

    # The message turn creates the thread and holds the per-thread lease inside run_tool.
    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await asyncio.wait_for(tools.first_entered.wait(), timeout=2.0)

    # The event submits behind the FIFO while the message turn holds the lease.
    await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    # The event turn cannot have run yet — it is queued behind the in-flight message.
    assert tools.order == ["message"]

    tools.gate.set()
    await _settle()
    assert tools.order == ["message", "event"]  # the event ran only after the message finished


async def test_start_expr_sees_turn_and_event(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(start_expr="{tid: .turn.id, ik: .turn.inbound.kind, ev: .event}")
    _wire(monkeypatch, FakeManager(route), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    result = await turn_module.submit_event(
        "tool-line",
        _event_submission(address="+15550002222", event=_event(payload={"n": 1})),
        "svc-int",
        client_connected=_connected,
    )
    await _settle()

    kwargs = tools.calls[-1]["arguments"]
    assert kwargs["tid"] == result.message_id
    assert kwargs["ik"] == "event"
    assert kwargs["ev"] == {"id": "evt-1", "kind": "provider.update", "payload": {"n": 1}}


class _PayloadArgSupport:
    def __init__(self, payload_arg: str) -> None:
        self.payload_arg = payload_arg


class _BindTools:
    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    async def get_tool(self, key: str) -> Tool:
        return self._tool


class _BindAgents:
    def all_agents(self) -> dict:
        return {}


class _BindPresets:
    def __init__(self, support: object) -> None:
        self._support = support

    def input_schema_support(self, base_tool: str) -> object:
        return self._support


class _BindApp:
    def __init__(self, tool: Tool, support: object) -> None:
        self.tools = _BindTools(tool)
        self.agents = _BindAgents()
        self.presets = _BindPresets(support)


async def _payload_arg_preset_tool(received: dict) -> Tool:
    """A real preset bind of a base tool that declares a single ``payload_arg`` (here
    ``run_kwargs``): the caller object is validated against a permissive input_schema and
    routed under that arg — exactly how a preset target receives a conversation turn's
    kwargs, whatever the consumer names its argument."""

    def relay(run_kwargs: dict) -> dict:
        received.clear()
        received.update(run_kwargs)
        return {"echo": run_kwargs}

    base = Tool.from_function(relay, name="relay")
    return await preset_bind(
        cast("TaiApp", _BindApp(base, _PayloadArgSupport("run_kwargs"))),
        "relay",
        {},
        name="conv-preset",
        input_schema={"type": "object"},
    )


class _PresetTools:
    """Runs the conversation turn's kwargs through the real preset bind."""

    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    async def run_tool(self, key: str, arguments: dict, *, offload_sync: bool = False, extras=None):
        result = await self._tool.run(arguments)
        return result.structured_content


async def test_preset_target_sees_turn_and_event_under_its_payload_arg(env, monkeypatch):
    channel = FakeChannel()
    route = _tool_channel_route(target_name="relay", reply_expr=".echo.turn.id")
    _wire(monkeypatch, FakeManager(route), channel)
    thread_id = await _seed_channel_thread(monkeypatch, env, channel)

    received: dict = {}
    tool = await _payload_arg_preset_tool(received)
    monkeypatch.setattr(accessors_module, "_tools", lambda: _PresetTools(tool))

    result = await turn_module.submit_event(
        "tool-line",
        _event_submission(address="+15550002222", event=_event(payload={"n": 1})),
        "svc-int",
        client_connected=_connected,
    )
    await _settle()

    # The preset's base tool received the whole turn payload under its payload_arg.
    assert received["turn"] == {
        "id": result.message_id,
        "inbound": {"id": "evt-1", "kind": "event", "source": "event:provider.update"},
        "subject": {
            "target_kind": "tool",
            "target_name": "relay",
            "person": None,
            "thread": thread_id,
            "locale": None,
        },
    }
    assert received["event"] == {"id": "evt-1", "kind": "provider.update", "payload": {"n": 1}}
    assert received["message"] is None
    # The reply mapped .echo.turn.id back through, proving the id round-tripped the flow.
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.answer == result.message_id


async def test_a_rate_capped_event_never_burns_the_idempotency_key(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    tools = _wire_tool(monkeypatch, lambda kw: "ok")

    # Drain the event caller's api bucket, so the FIRST submission is rate-capped.
    caps = caps_module.get_turn_caps()
    assert caps.admit_address(keys_module._api_bucket_key("tool-line", "svc-int")) is AddressAdmission.ADMIT
    submission = _event_submission(address="+15550002222", event=_event(event_id="evt-99"))
    with pytest.raises(caps_module.AddressRateLimitedError):
        await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)

    # The refused admission wrote nothing — the key is unclaimed.
    assert await _store().get_event_owner("tool-line", "evt-99") is None

    # A retry with a fresh budget runs exactly one turn (the key was never burned).
    caps_module._CAPS_CACHE.clear()
    result = await turn_module.submit_event("tool-line", submission, "svc-int", client_connected=_connected)
    await _settle()
    assert len(tools.calls) == 1
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.answer_status == "answered"


async def test_an_api_address_from_another_principal_is_a_404_while_its_thread_id_reaches_it(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    _wire_tool(monkeypatch, lambda kw: "ok")
    # alice opens an api thread naming end user u-7.
    seed = await turn_module.submit_api_message(
        "tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected
    )
    await _settle()

    # bob addressing the same end-user id composes a DIFFERENT (bob-qualified) thread → 404.
    with pytest.raises(turn_module.ThreadNotFoundError):
        await turn_module.submit_event(
            "tool-api", _event_submission(address="u-7", event=_event()), "bob", client_connected=_connected
        )

    # bob CAN reach alice's thread by its listed thread_id — the trusted-integration surface.
    result = await turn_module.submit_event(
        "tool-api", _event_submission(thread_id=seed.thread_id, event=_event()), "bob", client_connected=_connected
    )
    await _settle()
    record = await _store().get_record(result.message_id)
    assert record is not None
    # The record satisfies _new_record's api-door invariant: it carries the thread's
    # qualifying principal (alice), and records bob as the authorizer.
    assert record.caller_principal == "alice"
    assert record.submitted_by == "bob"


async def test_event_in_manual_mode_is_recorded_and_runs_no_turn(env, monkeypatch):
    from tai42_skeleton.conversations.mode import ConversationModeStore

    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    await _seed_channel_thread(monkeypatch, env, channel)
    await ConversationModeStore(ConversationsSettings()).set_mode("bridge:tool-line:+15550002222", "manual")
    tools = _wire_tool(monkeypatch, lambda kw: "should not run")

    result = await turn_module.submit_event(
        "tool-line", _event_submission(address="+15550002222", event=_event()), "svc-int", client_connected=_connected
    )
    await _settle()

    # Manual mode suppresses the turn exactly as for a message: the event is recorded silent.
    assert tools.calls == []
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.inbound_kind == "event"
    assert record.delivery_status is DeliveryStatus.SILENT


async def test_event_on_an_unknown_route_is_refused(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_channel_route()))
    with pytest.raises(turn_module.ConversationRouteResolutionError):
        await turn_module.submit_event(
            "no-such-route", _event_submission(address="x", event=_event()), "svc-int", client_connected=_connected
        )


async def test_event_with_no_caller_principal_is_refused(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_channel_route()))
    with pytest.raises(turn_module.UnauthenticatedApiCallerError):
        await turn_module.submit_event(
            "tool-line", _event_submission(address="x", event=_event()), None, client_connected=_connected
        )
