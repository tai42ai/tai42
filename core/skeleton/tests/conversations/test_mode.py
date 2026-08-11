"""The per-thread conversation-mode store, the effective-mode fold, the current-thread
feature body (turn-context keyed), and the reclamation of a mode override with its thread.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from tai42_contract.agent import Agent
from tai42_contract.conversations import ConversationRoute, Person, PersonAddress

from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.mode import (
    ConversationModeStore,
    NoBridgeTurnError,
    default_mode,
    effective_mode,
    set_current_thread_mode,
)
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn_context import BridgeTurnContext, bridge_turn_context
from tai42_skeleton.operations.errors import NotSupportedError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


def _turn_context(thread_id: str = "bridge:line:+15550002222") -> BridgeTurnContext:
    return BridgeTurnContext(
        thread_id=thread_id,
        route_name="line",
        channel="twilio",
        our_identity="+15550001111",
        client_address="+15550002222",
    )


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    redis = FakeRecordRedis()
    monkeypatch.setattr(mode_module, "client_ctx", make_record_client_ctx(redis))
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(redis))
    return redis


def _store() -> ConversationModeStore:
    return ConversationModeStore(ConversationsSettings())


def _route(initial_mode: str = "agent") -> ConversationRoute:
    return ConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
        execution_key_fingerprint="fp-1",
    )


def _route_named(route_name: str, initial_mode: str, our_identity: str) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity=our_identity,
        initial_mode=initial_mode,  # pyright: ignore[reportArgumentType]
        execution_key_fingerprint="fp-1",
    )


class _AgentInput(BaseModel):
    user_message: str = ""


class _MemoryAgent(Agent):
    tool_name = "echo"
    ToolInput = _AgentInput

    async def run(self, *, user_message: str = "", **kwargs):
        return ""

    async def append_thread_messages(self, *, thread_id, messages, **kwargs) -> None:
        return None


class FakeManager:
    def __init__(self, *routes: ConversationRoute) -> None:
        self._routes = {r.route_name: r for r in routes}

    async def get_route(self, name: str):
        return self._routes.get(name)

    async def list_routes(self):
        return dict(self._routes)


def _two_route_person() -> Person:
    return Person(
        person_id="p9",
        target_kind="agent",
        target_name="echo",
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


def _wire_person(monkeypatch, person: Person) -> None:
    class _FakePersonStore:
        def __init__(self, settings) -> None:
            pass

        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

    monkeypatch.setattr(persons_module, "ConversationPersonStore", _FakePersonStore)


def _wire_two_route_manager(monkeypatch, *, chat_mode: str, chat_b_mode: str) -> None:
    manager = FakeManager(
        _route_named("chat", chat_mode, "+15550001111"),
        _route_named("chat-b", chat_b_mode, "+15550003333"),
    )
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)


async def test_set_get_delete_round_trip(fake) -> None:
    store = _store()
    assert await store.get_mode("bridge:line:+1") is None
    assert await store.set_mode("bridge:line:+1", "manual") == "manual"
    assert await store.get_mode("bridge:line:+1") == "manual"
    assert await store.delete_mode("bridge:line:+1") is True
    assert await store.get_mode("bridge:line:+1") is None
    assert await store.delete_mode("bridge:line:+1") is False


async def test_set_refuses_an_unknown_value(fake) -> None:
    with pytest.raises(ValueError, match="conversation mode must be one of"):
        await _store().set_mode("bridge:line:+1", "auto")


async def test_get_raises_on_a_corrupt_stored_value(fake) -> None:
    # A value no set could write is corruption, surfaced loudly rather than coerced.
    fake._strings[ConversationsSettings().mode_key("bridge:line:+1")] = "auto"
    with pytest.raises(ValueError, match="conversation mode must be one of"):
        await _store().get_mode("bridge:line:+1")


async def test_effective_mode_prefers_the_override_then_the_route_default(fake) -> None:
    route = _route(initial_mode="agent")
    assert await effective_mode(route, "bridge:line:+1") == "agent"
    await _store().set_mode("bridge:line:+1", "manual")
    assert await effective_mode(route, "bridge:line:+1") == "manual"
    # A route defaulting to manual with no override reads manual.
    assert await effective_mode(_route(initial_mode="manual"), "bridge:line:+other") == "manual"


async def test_set_current_thread_mode_writes_the_turn_contexts_thread(fake, monkeypatch) -> None:
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: FakeManager(_route()))
    monkeypatch.setattr(turn_module, "_agent_registry", lambda: {"echo": _MemoryAgent()})
    with bridge_turn_context(_turn_context("bridge:line:+15550002222")):
        stored = await set_current_thread_mode("manual")

    assert stored == "manual"
    assert await _store().get_mode("bridge:line:+15550002222") == "manual"


async def test_set_current_thread_mode_outside_a_turn_raises(fake) -> None:
    # No bridge context bound: the tool refuses loudly rather than acting on nothing.
    with pytest.raises(NoBridgeTurnError, match="outside a bridge turn"):
        await set_current_thread_mode("manual")


async def test_set_current_thread_mode_validates_the_mode(fake) -> None:
    with bridge_turn_context(_turn_context()), pytest.raises(ValueError, match="conversation mode must be one of"):
        await set_current_thread_mode("auto")


async def test_construction_refuses_without_the_backend(monkeypatch) -> None:
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    with pytest.raises(NotSupportedError):
        ConversationModeStore(ConversationsSettings())


# -- reclamation with the thread ---------------------------------------------


async def _seed_record(store: ConversationRecordStore, *, message_id: str, route_name: str, thread_id: str) -> None:
    now = time.time()
    await store.create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=thread_id,
            client_address="+15550002222",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def test_drop_thread_reclaims_the_mode_override(fake) -> None:
    settings = ConversationsSettings()
    store = ConversationRecordStore(settings)
    fake.seed_route("line")
    await _seed_record(store, message_id="m0", route_name="line", thread_id="bridge:line:+15550002222")
    await _store().set_mode("bridge:line:+15550002222", "manual")
    assert settings.mode_key("bridge:line:+15550002222") in fake._strings

    await store.drop_thread("line", "bridge:line:+15550002222")

    assert settings.mode_key("bridge:line:+15550002222") not in fake._strings


async def test_drop_route_threads_reclaims_every_thread_mode_override(fake) -> None:
    settings = ConversationsSettings()
    store = ConversationRecordStore(settings)
    fake.seed_route("line")
    await _seed_record(store, message_id="m0", route_name="line", thread_id="bridge:line:+15550002222")
    await _seed_record(store, message_id="m1", route_name="line", thread_id="bridge:line:+15550003333")
    await _store().set_mode("bridge:line:+15550002222", "manual")
    await _store().set_mode("bridge:line:+15550003333", "agent")

    await store.drop_route_threads("line")

    assert settings.mode_key("bridge:line:+15550002222") not in fake._strings
    assert settings.mode_key("bridge:line:+15550003333") not in fake._strings


# -- person-thread default (spans several routes) ----------------------------

_PERSON_THREAD = "bridge:@person:p9"


async def test_default_mode_person_thread_is_manual_if_any_route_is_manual(fake, monkeypatch) -> None:
    _wire_person(monkeypatch, _two_route_person())
    _wire_two_route_manager(monkeypatch, chat_mode="agent", chat_b_mode="manual")
    assert await default_mode(_route(), _PERSON_THREAD) == "manual"
    # With no override the effective mode folds to the same default.
    assert await effective_mode(_route(), _PERSON_THREAD) == "manual"


async def test_default_mode_person_thread_is_agent_if_every_route_is_agent(fake, monkeypatch) -> None:
    _wire_person(monkeypatch, _two_route_person())
    _wire_two_route_manager(monkeypatch, chat_mode="agent", chat_b_mode="agent")
    assert await default_mode(_route(), _PERSON_THREAD) == "agent"
    assert await effective_mode(_route(), _PERSON_THREAD) == "agent"


async def test_person_thread_override_still_wins_over_the_folded_default(fake, monkeypatch) -> None:
    _wire_person(monkeypatch, _two_route_person())
    _wire_two_route_manager(monkeypatch, chat_mode="manual", chat_b_mode="manual")
    await _store().set_mode(_PERSON_THREAD, "agent")
    assert await effective_mode(_route(), _PERSON_THREAD) == "agent"


# -- lifetime: the override never outlives the conversation -------------------


async def test_set_mode_writes_the_retention_ttl(fake) -> None:
    settings = ConversationsSettings()
    await _store().set_mode("bridge:line:+1", "manual")
    assert fake.ttl_ms[settings.mode_key("bridge:line:+1")] == settings.answer_retention_ttl_seconds * 1000


async def test_refresh_ttl_extends_a_live_override(fake) -> None:
    settings = ConversationsSettings()
    await _store().set_mode("bridge:line:+1", "manual")
    fake.advance(settings.answer_retention_ttl_seconds / 2)  # half the window elapses

    assert await _store().refresh_ttl("bridge:line:+1") is True
    assert fake.ttl_ms[settings.mode_key("bridge:line:+1")] == settings.answer_retention_ttl_seconds * 1000


async def test_refresh_ttl_is_a_noop_when_no_override(fake) -> None:
    # No override set: refresh reports nothing to extend and never creates the key.
    assert await _store().refresh_ttl("bridge:line:+1") is False
    assert ConversationsSettings().mode_key("bridge:line:+1") not in fake._strings


async def test_override_expires_to_the_route_default(fake) -> None:
    settings = ConversationsSettings()
    await _store().set_mode("bridge:line:+1", "manual")
    fake.advance(settings.answer_retention_ttl_seconds + 1)  # the whole window lapses

    assert await _store().get_mode("bridge:line:+1") is None
    # A returning address reads the route default again once the override has aged out.
    assert await effective_mode(_route(initial_mode="agent"), "bridge:line:+1") == "agent"


async def test_refresh_never_resurrects_a_deleted_override(fake) -> None:
    await _store().set_mode("bridge:line:+1", "manual")
    assert await _store().delete_mode("bridge:line:+1") is True

    assert await _store().refresh_ttl("bridge:line:+1") is False
    assert ConversationsSettings().mode_key("bridge:line:+1") not in fake._strings
