"""The thread-control operations: operator send (target resolution + guards) and the mode
get/set doors (effective mode, source, and the thread-belongs-to-route guard).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError
from tai42_contract.agent import Agent
from tai42_contract.channels import ChannelTemplate, OptionSection, ReplyOption
from tai42_contract.conversations import ConversationRoute, Person, PersonAddress
from tai42_contract.interactions import LocationElement, MediaItem, MediaKind

from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.mode import ConversationModeStore
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, NotFoundError, NotSupportedError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_ROUTE_THREAD = "bridge:chat:+15550002222"
_PERSON_THREAD = "bridge:@person:p1"


def _channel_route(route_name: str = "chat", initial_mode: str = "agent", our_identity: str = "+15550001111"):
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
        execution_key_fingerprint="fp-1",
    )


class FakeManager:
    def __init__(self, *routes: ConversationRoute) -> None:
        self._routes = {r.route_name: r for r in routes}

    async def list_routes(self):
        return dict(self._routes)

    async def get_route(self, name: str):
        return self._routes.get(name)


class _AgentInput(BaseModel):
    user_message: str = ""


class _MemoryAgent(Agent):
    """A memory-holding agent — implements ``append_thread_messages``, so its route serves
    manual mode."""

    tool_name = "relay"
    ToolInput = _AgentInput

    async def run(self, *, user_message: str = "", **kwargs):
        return ""

    async def append_thread_messages(self, *, thread_id, messages, **kwargs) -> None:
        return None


class _MemorylessAgent(Agent):
    """An agent that leaves ``append_thread_messages`` the ABC default, so its route cannot
    serve manual mode."""

    tool_name = "relay"
    ToolInput = _AgentInput

    async def run(self, *, user_message: str = "", **kwargs):
        return ""


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    fake = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(mode_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(thread_lease_module, "client_ctx", make_record_client_ctx(fake))
    manager = FakeManager(_channel_route())
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: manager)
    # The route's ``relay`` agent holds thread memory by default, so manual mode is permitted.
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"relay": _MemoryAgent()})

    async def _caller():
        return SimpleNamespace(caller_id="op-1", is_admin=True)

    monkeypatch.setattr(ops, "resolve_caller", _caller)
    return SimpleNamespace(redis=fake, manager=manager)


def _mode_store() -> ConversationModeStore:
    return ConversationModeStore(ConversationsSettings())


async def _seed_record(
    *, message_id: str, route_name: str, thread_id: str, address: str, channel: str = "twilio"
) -> None:
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=thread_id,
            client_address=address,
            channel=channel,
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask",
            answer_status="answered",
            answer="hi",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


def _capture_operator_send(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _fake(
        *,
        route,
        thread_id,
        client_address,
        text,
        operator_principal,
        media=None,
        template=None,
        options=None,
        location=None,
        sections=None,
        header=None,
        footer=None,
        schema=None,
    ):
        calls.append(
            {
                "route_name": route.route_name,
                "thread_id": thread_id,
                "client_address": client_address,
                "text": text,
                "operator_principal": operator_principal,
                "media": media,
                "template": template,
                "options": options,
                "location": location,
                "sections": sections,
                "header": header,
                "footer": footer,
                "schema": schema,
            }
        )
        return "msg-1"

    monkeypatch.setattr(turn_module, "operator_send", _fake)
    return calls


# -- operator send: route-keyed thread ---------------------------------------


async def test_send_route_keyed_resolves_the_embedded_address(wired, monkeypatch):
    calls = _capture_operator_send(monkeypatch)

    result = await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "on it")

    assert result == {"message_id": "msg-1", "thread_id": _ROUTE_THREAD}
    assert calls == [
        {
            "route_name": "chat",
            "thread_id": _ROUTE_THREAD,
            "client_address": "+15550002222",
            "text": "on it",
            "operator_principal": "op-1",
            "media": None,
            "template": None,
            "options": None,
            "location": None,
            "sections": None,
            "header": None,
            "footer": None,
            "schema": None,
        }
    ]


async def test_send_template_threads_through_as_channel_template(wired, monkeypatch):
    # The operator send accepts a template dict and hands operator_send the coerced
    # ChannelTemplate — the same out-of-window richer-send form the notify door carries.
    calls = _capture_operator_send(monkeypatch)

    result = await ops.send_conversation_thread_message(
        "chat",
        _ROUTE_THREAD,
        "your order shipped",
        template={"name": "status_update", "language": "en_US", "body_parameters": ["A-42"]},
    )

    assert result == {"message_id": "msg-1", "thread_id": _ROUTE_THREAD}
    assert calls[0]["template"] == ChannelTemplate(name="status_update", language="en_US", body_parameters=["A-42"])
    assert calls[0]["media"] is None
    assert calls[0]["options"] is None


async def test_send_invalid_template_is_a_400(wired, monkeypatch):
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="invalid rich-send fields"):
        await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "on it", template={"name": "   "})
    assert calls == []


async def test_send_media_and_template_is_a_400(wired, monkeypatch):
    # The contract's media/template mutual exclusion holds on the operator send — refused
    # before anything is written or delivered.
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="mutually exclusive"):
        await ops.send_conversation_thread_message(
            "chat",
            _ROUTE_THREAD,
            "on it",
            media=[{"kind": "image", "url": "https://cdn.example/x.png"}],
            template={"name": "status_update", "language": "en_US"},
        )
    assert calls == []


async def test_send_options_and_template_is_a_400(wired, monkeypatch):
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="mutually exclusive"):
        await ops.send_conversation_thread_message(
            "chat",
            _ROUTE_THREAD,
            "on it",
            template={"name": "status_update", "language": "en_US"},
            options=[{"kind": "reply", "text": "Thanks"}],
        )
    assert calls == []


async def test_send_schema_threads_through_to_operator_send(wired, monkeypatch):
    # The operator send accepts an ask-less form's answer schema and hands operator_send
    # the dict unchanged — the same keyword-safe threading template took.
    calls = _capture_operator_send(monkeypatch)
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}

    result = await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "fill this in", schema=schema)

    assert result == {"message_id": "msg-1", "thread_id": _ROUTE_THREAD}
    assert calls[0]["schema"] == schema
    assert calls[0]["template"] is None


async def test_send_full_vocabulary_threads_through_to_operator_send(wired, monkeypatch):
    # Full parity with the flow answer path: location/sections/header/footer are coerced to the
    # typed contract models and handed to operator_send alongside the earlier rich fields.
    calls = _capture_operator_send(monkeypatch)

    result = await ops.send_conversation_thread_message(
        "chat",
        _ROUTE_THREAD,
        "a titled card",
        location={"latitude": 51.5, "longitude": -0.12, "name": "HQ"},
        sections=[{"title": "Pick", "rows": [{"kind": "reply", "text": "Item A"}]}],
        header={"kind": "image", "url": "https://cdn.example/banner.png"},
        footer="powered by us",
    )

    assert result == {"message_id": "msg-1", "thread_id": _ROUTE_THREAD}
    assert calls[0]["location"] == LocationElement(latitude=51.5, longitude=-0.12, name="HQ")
    assert calls[0]["sections"] == [OptionSection(title="Pick", rows=[ReplyOption(text="Item A")])]
    assert calls[0]["header"] == MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/banner.png")
    assert calls[0]["footer"] == "powered by us"


async def test_send_options_and_sections_is_a_400(wired, monkeypatch):
    # The shared composition matrix (options XOR sections) is enforced on the operator send —
    # refused before anything is written or delivered.
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="mutually exclusive"):
        await ops.send_conversation_thread_message(
            "chat",
            _ROUTE_THREAD,
            "on it",
            options=[{"kind": "reply", "text": "A"}],
            sections=[{"title": "Pick", "rows": [{"kind": "reply", "text": "B"}]}],
        )
    assert calls == []


async def test_send_header_without_choice_surface_is_a_400(wired, monkeypatch):
    # A header/footer COMPOSES an interactive message, so it requires options or sections — a
    # bare header is a loud 400, enforced by the shared composition matrix.
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="header requires options or sections"):
        await ops.send_conversation_thread_message(
            "chat",
            _ROUTE_THREAD,
            "on it",
            header={"kind": "image", "url": "https://cdn.example/banner.png"},
        )
    assert calls == []


async def test_send_schema_and_template_is_a_400(wired, monkeypatch):
    # The contract's schema/template mutual exclusion holds on the operator send — refused
    # before anything is written or delivered.
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="mutually exclusive"):
        await ops.send_conversation_thread_message(
            "chat",
            _ROUTE_THREAD,
            "fill this in",
            template={"name": "status_update", "language": "en_US"},
            schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
    assert calls == []


async def test_send_schema_and_options_is_a_400(wired, monkeypatch):
    # One message carries ONE interactive surface: schema + options is the contract's
    # exclusivity refusal, mapped to a clean 400.
    calls = _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="mutually exclusive"):
        await ops.send_conversation_thread_message(
            "chat",
            _ROUTE_THREAD,
            "fill this in",
            options=[{"kind": "reply", "text": "Thanks"}],
            schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
    assert calls == []


async def test_send_explicit_address_must_match_the_route_keyed_thread(wired, monkeypatch):
    _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="not the address of thread"):
        await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "on it", address="+15550009999")


async def test_send_blank_text_is_a_400(wired, monkeypatch):
    _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="non-blank message"):
        await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "   ")


async def test_send_present_but_blank_address_is_a_400(wired, monkeypatch):
    # A present-but-blank address canonicalizes to nothing (a ValueError on the address path);
    # it is a loud 400, never a bare ValueError escaping as a 500.
    _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="invalid address"):
        await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "on it", address="")


async def test_send_thread_off_route_is_a_400(wired, monkeypatch):
    _capture_operator_send(monkeypatch)
    with pytest.raises(BadRequestError, match="not a thread of route"):
        await ops.send_conversation_thread_message("chat", "bridge:other:+15550002222", "on it")


async def test_send_unknown_route_is_a_404(wired, monkeypatch):
    _capture_operator_send(monkeypatch)
    with pytest.raises(NotFoundError):
        await ops.send_conversation_thread_message("nope", _ROUTE_THREAD, "on it")


async def test_send_unauthenticated_caller_is_a_501(wired, monkeypatch):
    _capture_operator_send(monkeypatch)

    async def _anon():
        return SimpleNamespace(caller_id=None, is_admin=True)

    monkeypatch.setattr(ops, "resolve_caller", _anon)
    with pytest.raises(NotSupportedError, match="authenticated caller principal"):
        await ops.send_conversation_thread_message("chat", _ROUTE_THREAD, "on it")


# -- operator send: person thread --------------------------------------------


def _person() -> Person:
    return Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat"],
                channel="twilio",
                our_identity="+15550001111",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel",
                routes=["chat-b"],
                channel="whatsapp",
                our_identity="+15550003333",
                address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )


def _wire_person(monkeypatch, person: Person | None) -> None:
    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person is not None and person_id == person.person_id else None

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())


async def test_send_person_default_targets_the_newest_record(wired, monkeypatch):
    calls = _capture_operator_send(monkeypatch)
    _wire_person(monkeypatch, _person())
    # The person spans chat + chat-b; add chat-b to the manager so the newest record's route
    # resolves live.
    wired.manager._routes["chat-b"] = _channel_route("chat-b", our_identity="+15550003333")
    wired.redis.seed_route("chat")
    wired.redis.seed_route("chat-b")
    await _seed_record(message_id="pa", route_name="chat", thread_id=_PERSON_THREAD, address="+1000")
    time.sleep(0.01)
    await _seed_record(
        message_id="pb", route_name="chat-b", thread_id=_PERSON_THREAD, address="+2000", channel="whatsapp"
    )

    await ops.send_conversation_thread_message("chat", _PERSON_THREAD, "on it")

    # The newest record (pb, on chat-b) governs the send target.
    assert calls[0]["route_name"] == "chat-b"
    assert calls[0]["client_address"] == "+2000"


async def test_send_person_explicit_address_must_be_one_of_the_persons(wired, monkeypatch):
    calls = _capture_operator_send(monkeypatch)
    _wire_person(monkeypatch, _person())
    wired.manager._routes["chat-b"] = _channel_route("chat-b", our_identity="+15550003333")

    await ops.send_conversation_thread_message("chat", _PERSON_THREAD, "on it", address="+1000")
    assert calls[0]["client_address"] == "+1000"
    assert calls[0]["route_name"] == "chat"

    with pytest.raises(BadRequestError, match="not one of the thread's person addresses"):
        await ops.send_conversation_thread_message("chat", _PERSON_THREAD, "on it", address="+9999")


async def test_send_empty_person_thread_without_address_is_a_400(wired, monkeypatch):
    _capture_operator_send(monkeypatch)
    _wire_person(monkeypatch, _person())
    wired.manager._routes["chat-b"] = _channel_route("chat-b", our_identity="+15550003333")

    with pytest.raises(BadRequestError, match="no record to infer a send address"):
        await ops.send_conversation_thread_message("chat", _PERSON_THREAD, "on it")


async def test_send_person_thread_off_route_is_a_404(wired, monkeypatch):
    _capture_operator_send(monkeypatch)
    _wire_person(monkeypatch, _person())
    with pytest.raises(NotFoundError):
        await ops.send_conversation_thread_message("nope-route", _PERSON_THREAD, "on it")


async def test_send_corrupt_person_row_is_a_500_not_a_400(wired, monkeypatch):
    # A corrupt stored person row makes the store read raise a pydantic ``ValidationError`` — a
    # ``ValueError`` subclass. That is server-side corruption (a loud 500), never mislabeled as
    # a client 400 with pydantic's dump in the body: only a malformed address maps to a 400.
    _capture_operator_send(monkeypatch)

    class _CorruptPersonStore:
        async def get_by_id(self, person_id: str) -> Person:
            return Person.model_validate_json("{")

    monkeypatch.setattr(ops, "_person_store", lambda: _CorruptPersonStore())

    with pytest.raises(ValidationError):
        await ops.send_conversation_thread_message("chat", _PERSON_THREAD, "on it")


# -- mode doors --------------------------------------------------------------


async def test_get_mode_reads_the_route_default_when_no_override(wired):
    assert await ops.get_conversation_thread_mode("chat", _ROUTE_THREAD) == {"mode": "agent", "source": "route"}


async def test_get_mode_reads_the_thread_override(wired):
    await _mode_store().set_mode(_ROUTE_THREAD, "manual")
    assert await ops.get_conversation_thread_mode("chat", _ROUTE_THREAD) == {"mode": "manual", "source": "thread"}


async def test_set_mode_writes_the_override(wired):
    result = await ops.set_conversation_thread_mode("chat", _ROUTE_THREAD, "manual")
    assert result == {"route_name": "chat", "thread_id": _ROUTE_THREAD, "mode": "manual", "source": "thread"}
    assert await _mode_store().get_mode(_ROUTE_THREAD) == "manual"


async def test_set_mode_rejects_an_unknown_mode(wired):
    with pytest.raises(BadRequestError, match="mode must be one of"):
        await ops.set_conversation_thread_mode("chat", _ROUTE_THREAD, "auto")


async def test_set_manual_on_a_memoryless_agent_is_allowed(wired, monkeypatch):
    # Manual is valid for EVERY target: the set-mode door writes the override without inspecting
    # the target's thread memory. A memoryless agent (leaves append_thread_messages the ABC
    # default) is accepted for manual just the same — its manual inbound records silently later.
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"relay": _MemorylessAgent()})
    result = await ops.set_conversation_thread_mode("chat", _ROUTE_THREAD, "manual")
    assert result["mode"] == "manual"
    assert await _mode_store().get_mode(_ROUTE_THREAD) == "manual"


async def test_set_agent_mode_needs_no_memory_check(wired, monkeypatch):
    # Setting a thread's mode never checks the target's thread memory — agent mode has nothing
    # to check, and manual is valid for every target too.
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"relay": _MemorylessAgent()})
    result = await ops.set_conversation_thread_mode("chat", _ROUTE_THREAD, "agent")
    assert result["mode"] == "agent"


async def test_mode_doors_reject_a_thread_off_route(wired):
    with pytest.raises(BadRequestError, match="not a thread of route"):
        await ops.get_conversation_thread_mode("chat", "bridge:other:+1")
    with pytest.raises(BadRequestError, match="not a thread of route"):
        await ops.set_conversation_thread_mode("chat", "bridge:other:+1", "manual")


async def test_mode_doors_reject_a_blank_thread_id(wired):
    with pytest.raises(BadRequestError, match="non-blank thread"):
        await ops.get_conversation_thread_mode("chat", "   ")


async def test_mode_get_unknown_route_is_a_404(wired):
    with pytest.raises(NotFoundError):
        await ops.get_conversation_thread_mode("nope", _ROUTE_THREAD)


# -- person-thread default mode (spans several routes) -----------------------


async def test_get_mode_person_thread_defaults_manual_if_any_route_is_manual(wired, monkeypatch):
    # The person spans chat (agent) and chat-b (manual); the aggregated thread's no-override
    # default is manual, since one operator control decision covers every channel it spans.
    _wire_person(monkeypatch, _person())
    wired.manager._routes["chat"] = _channel_route("chat", initial_mode="agent")
    wired.manager._routes["chat-b"] = _channel_route("chat-b", initial_mode="manual", our_identity="+15550003333")

    assert await ops.get_conversation_thread_mode("chat", _PERSON_THREAD) == {"mode": "manual", "source": "route"}


async def test_get_mode_person_thread_defaults_agent_if_every_route_is_agent(wired, monkeypatch):
    _wire_person(monkeypatch, _person())
    wired.manager._routes["chat"] = _channel_route("chat", initial_mode="agent")
    wired.manager._routes["chat-b"] = _channel_route("chat-b", initial_mode="agent", our_identity="+15550003333")

    assert await ops.get_conversation_thread_mode("chat", _PERSON_THREAD) == {"mode": "agent", "source": "route"}
