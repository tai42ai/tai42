"""The record create carries the inbound claim and the thread-mode refresh in its one script.

The store-level cases run the REAL Lua against ``fakeredis[lua]`` with a command counter; the
door-level cases drive every create site through the suite's record fake and count the scripts
and the mode-key ``EXPIRE`` each site sends.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission
from tai42_contract.interactions import PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn.operator_send import operator_send
from tai42_skeleton.runs.chokepoint import delivery_fire

from .conftest import (
    EchoAgent,
    FakeChannel,
    FakeManager,
    _accepting_callback,
    _all_record_ids,
    _api_route,
    _channel_route,
    _connected,
    _settle,
    _tool_api_route,
    _tool_channel_route,
    _wire,
    _wire_tool,
)
from .conftest import (
    _store as _conversation_store,
)
from .fake_record_redis import FakeRecordRedis

_THREAD = "bridge:line:+15550002222"


class _CountingRedis(aioredis.FakeRedis):
    """A Lua-executing fake that records the name of every command it is sent."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[str] = []

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        self.commands.append(str(args[0]).upper())
        return await super().execute_command(*args, **options)


@pytest.fixture
async def lua_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_CountingRedis]:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    client = _CountingRedis(decode_responses=True)
    await client.set(ConversationsSettings().route_key("line"), "{}")

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield client

    monkeypatch.setattr(records_module, "client_ctx", fake_client_ctx)
    try:
        yield client
    finally:
        await client.aclose()


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _s() -> ConversationsSettings:
    return ConversationsSettings()


def _intake(message_id: str) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=_THREAD,
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id="PID1",
        origin="client",
        inbound_text="ask",
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )


async def _index_members(client: _CountingRedis) -> set[str]:
    members: set[str] = set()
    for status in DeliveryStatus:
        members.update(await client.zrange(_s().status_index_key(status.value), 0, -1))
    return members


# -- the one script ----------------------------------------------------------------------


async def test_a_fresh_claim_writes_the_record_the_claim_and_refreshes_the_mode_in_one_script(lua_redis):
    await lua_redis.set(_s().mode_key(_THREAD), "manual", ex=60)
    lua_redis.commands.clear()

    owner = await _store().create_record(
        _intake("m1"), intake_token="tok", claim_key=_s().dedupe_key("twilio", "PID1"), refresh_mode=True
    )

    assert owner == "m1"
    assert [c for c in lua_redis.commands if c in {"EVAL", "EVALSHA"}] == [lua_redis.commands[0]]
    assert len(lua_redis.commands) == 1
    assert await lua_redis.hget(_s().record_key("m1"), "delivery_status") == "accepted"
    assert await lua_redis.get(_s().dedupe_key("twilio", "PID1")) == "m1"
    assert 0 < await lua_redis.ttl(_s().dedupe_key("twilio", "PID1")) <= _s().inbound_dedupe_ttl_seconds
    assert await lua_redis.ttl(_s().mode_key(_THREAD)) > 60


async def test_a_lost_claim_writes_nothing_and_returns_the_owner(lua_redis):
    await lua_redis.set(_s().dedupe_key("twilio", "PID1"), "winner")
    await lua_redis.set(_s().mode_key(_THREAD), "manual", ex=60)

    owner = await _store().create_record(
        _intake("loser"), intake_token="tok", claim_key=_s().dedupe_key("twilio", "PID1"), refresh_mode=True
    )

    assert owner == "winner"
    assert await lua_redis.exists(_s().record_key("loser")) == 0
    assert "loser" not in await _index_members(lua_redis)
    assert await lua_redis.zrange(_s().thread_index_key("line", _THREAD), 0, -1) == []
    assert await lua_redis.get(_s().dedupe_key("twilio", "PID1")) == "winner"
    # The losing attempt does not refresh the thread's mode; the winner's own create did.
    assert await lua_redis.ttl(_s().mode_key(_THREAD)) <= 60


async def test_an_absent_mode_key_stays_absent(lua_redis):
    await _store().create_record(_intake("m1"), intake_token="tok", refresh_mode=True)

    assert await lua_redis.exists(_s().mode_key(_THREAD)) == 0


async def test_a_same_id_re_create_proceeds(lua_redis):
    await lua_redis.set(_s().dedupe_key("twilio", "PID1"), "m1")

    owner = await _store().create_record(_intake("m1"), intake_token="tok", claim_key=_s().dedupe_key("twilio", "PID1"))

    assert owner == "m1"
    assert await lua_redis.hget(_s().record_key("m1"), "delivery_status") == "accepted"


async def test_an_event_claim_uses_its_own_key_family(lua_redis):
    await lua_redis.set(_s().dedupe_key("line", "E1"), "channel-owner")

    owner = await _store().create_record(
        _intake("ev"), intake_token="tok", claim_key=_s().event_dedupe_key("line", "E1")
    )

    assert owner == "ev"
    assert await lua_redis.get(_s().event_dedupe_key("line", "E1")) == "ev"


async def test_the_route_missing_marker_and_warning_are_unchanged_under_a_claim(lua_redis, caplog):
    await lua_redis.delete(_s().route_key("line"))

    with caplog.at_level(logging.WARNING):
        owner = await _store().create_record(
            _intake("m1"), intake_token="tok", claim_key=_s().dedupe_key("twilio", "PID1"), refresh_mode=True
        )

    assert owner == "m1"
    assert await lua_redis.hget(_s().record_key("m1"), "route_missing") == "1"
    assert await lua_redis.get(_s().dedupe_key("twilio", "PID1")) == "m1"
    assert any("stopped routing" in r.getMessage() for r in caplog.records)


async def test_a_create_with_no_claim_returns_its_own_id(lua_redis):
    record = _intake("m1").model_copy(update={"delivery_status": DeliveryStatus.PENDING_DELIVERY})

    assert await _store().create_record(record) == "m1"


# -- every create site sends one script and nothing else ---------------------------------


class _SiteCounts:
    def __init__(self, fake: FakeRecordRedis) -> None:
        self.creates = 0
        self.claims = 0
        self.mode_expires = 0
        real_eval, real_expire = fake.eval, fake.expire
        mode_prefix = _s().mode_key("x")[:-1]

        async def eval_(script: str, numkeys: int, *keys_and_args: Any) -> Any:
            if "conversations:record:create" in script:
                self.creates += 1
            if "conversations:dedupe:claim" in script:
                self.claims += 1
            return await real_eval(script, numkeys, *keys_and_args)

        async def expire(key: str, seconds: int) -> bool:
            if key.startswith(mode_prefix):
                self.mode_expires += 1
            return await real_expire(key, seconds)

        fake.eval = eval_  # type: ignore[method-assign]
        fake.expire = expire  # type: ignore[method-assign]


@pytest.fixture
def sites(env: FakeRecordRedis) -> _SiteCounts:
    return _SiteCounts(env)


def _echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})


async def test_the_channel_door_creates_claims_and_refreshes_in_one_script(env, sites, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    _echo(monkeypatch)

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert (sites.creates, sites.claims, sites.mode_expires) == (1, 0, 0)


async def test_a_channel_redelivery_that_loses_the_claim_writes_no_record(env, sites, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    _echo(monkeypatch)
    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()
    # A concurrent attempt for the same pair commits between this attempt's fast-path read and
    # its create: the read finds no owner, the claim already stands when the create runs.
    await env.set(_s().dedupe_key("twilio", "PID2"), first)

    async def no_owner_yet(self: ConversationRecordStore, channel_name: str, provider_message_id: str) -> None:
        return None

    monkeypatch.setattr(ConversationRecordStore, "get_inbound_owner", no_owner_yet)

    owner = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()

    assert owner == first
    assert [n.message for n in channel.sends] == ["echo: hi"]
    assert sites.creates == 2
    assert await _all_record_ids(_conversation_store()) == [first]


async def test_both_shed_sites_create_and_claim_in_one_script(env, sites, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    _echo(monkeypatch)

    for index in range(3):  # a turn, then the slow-down reply, then a silent shed
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", f"PID{index}")
        await _settle()

    assert (sites.creates, sites.claims, sites.mode_expires) == (3, 0, 0)


async def test_the_api_door_creates_and_refreshes_in_one_script(env, sites, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    env.seed_route("chat")
    _echo(monkeypatch)
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=0, client_connected=_connected
    )
    await _settle()

    assert (sites.creates, sites.claims, sites.mode_expires) == (1, 0, 0)


async def test_the_operator_send_creates_and_refreshes_in_one_script(env, sites, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)
    _echo(monkeypatch)

    await operator_send(
        route=route, thread_id=_THREAD, client_address="+15550002222", text="on it", operator_principal="op-1"
    )
    await _settle()

    assert (sites.creates, sites.claims, sites.mode_expires) == (1, 0, 0)


async def test_the_agent_completion_creates_and_refreshes_in_one_script(env, sites, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)

    with delivery_fire("c-agent"):
        await turn_module.deliver_agent_completion(
            thread_id=_THREAD, completion_id="c-agent", result="done", status=PARK_COMPLETION_SUCCEEDED
        )
    await _settle()

    assert (sites.creates, sites.claims, sites.mode_expires) == (1, 0, 0)


async def test_the_tool_completion_creates_and_refreshes_in_one_script(env, sites, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route(reply_expr=".result.reply // null")), channel)
    env.seed_route("tool-line")

    with delivery_fire("c-tool"):
        await turn_module.deliver_tool_completion(
            delivery_thread_id="bridge:tool-line:+15550002222",
            completion_id="c-tool",
            result={"result": {"reply": "done"}},
            status=PARK_COMPLETION_SUCCEEDED,
        )
    await _settle()

    assert (sites.creates, sites.claims, sites.mode_expires) == (1, 0, 0)


async def test_the_event_door_creates_claims_and_refreshes_in_one_script(env, sites, monkeypatch):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    _wire_tool(monkeypatch, lambda kw: "ok")
    await turn_module.submit_api_message("tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected)
    await _settle()

    submission = ConversationEventSubmission(
        address="u-7", event=ConversationEvent(event_id="evt-1", kind="k", payload={})
    )
    first = await turn_module.submit_event("tool-api", submission, "alice", client_connected=_connected)
    await _settle()
    again = await turn_module.submit_event("tool-api", submission, "alice", client_connected=_connected)
    await _settle()

    assert again.message_id == first.message_id
    # The api message and the event: one script each. The redelivery is answered by the fast
    # path and creates nothing.
    assert (sites.creates, sites.claims, sites.mode_expires) == (2, 0, 0)
