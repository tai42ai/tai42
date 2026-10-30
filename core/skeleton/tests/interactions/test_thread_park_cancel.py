"""The thread→interaction reverse index and its cascade cancel.

An async ``ask`` park is stored keyed by interaction id alone, so a conversation
thread delete reaches it only through a reverse index; without one the park would
outlive its thread, the expiry reaper would fire a continuation into a deleted thread
(a delivery retry storm) and the channel correlation would stay muted until the ~24h
deadline. This suite proves that reverse index: a park bound to a thread joins
``thread-parks:{thread_id}``, the member is dropped wherever the interaction leaves pending
(answer / prune), and the whole-run kill's thread reach reads the reverse-index snapshot
(``thread_park_members``) and reconciles it (``reconcile_thread_park_members``) as it routes
each member through the kill seam.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    InteractionResponse,
    SuspendedInteraction,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
)
from tai42_contract.tools import tool_call_frame

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations.turn_context import BridgeTurnContext, bridge_turn_context
from tai42_skeleton.interactions import InteractionStore, ask
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings

_THREAD = "bridge:chat:+15550001111"
_OTHER_THREAD = "bridge:chat:+15559990000"


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # Set BEFORE any InteractionsSettings() is built, so the ask OFF-gate passes.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


def _park_request(
    store: InteractionStore, interaction_id: str, group_id: str, *, mode: str = "async", expiry_minutes: int = 60
) -> InteractionRequest:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=expiry_minutes) if mode == "async" else None
    return InteractionRequest(
        interaction_id=interaction_id,
        group_id=group_id,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(interaction_id),
        created_at=now,
        timeout_at=expiry if expiry is not None else now + timedelta(seconds=60),
        mode=mode,  # type: ignore[arg-type]
        continuation_tool="resume_tool" if mode == "async" else None,
        continuation_identity="svc-key" if mode == "async" else None,
        expiry_at=expiry,
    )


# -- store: write-on-park / clear-on-answer / clear-on-prune -------------------


async def test_async_park_with_thread_joins_the_reverse_index(fake_redis):
    store = InteractionStore("t:")
    await store.add(
        fake_redis, _park_request(store, "i1", "g1"), idle_ttl=86400, continuation_fingerprint="fp-1", thread_id=_THREAD
    )
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {"i1"}
    # The thread id is denormalized on the state hash so the terminal claims can read it.
    assert await fake_redis.hget(store.state_key("i1"), "thread_id") == _THREAD


async def test_async_park_without_thread_writes_no_index(fake_redis):
    # A background tool run parks with no bound thread — nothing is indexed.
    store = InteractionStore("t:")
    await store.add(fake_redis, _park_request(store, "i1", "g1"), idle_ttl=86400, continuation_fingerprint="fp-1")
    assert await fake_redis.hget(store.state_key("i1"), "thread_id") is None
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()


async def test_sync_question_with_thread_writes_no_index(fake_redis):
    # Only async parks index — a sync question is never orphaned (its caller blocks).
    store = InteractionStore("t:")
    await store.add(fake_redis, _park_request(store, "s1", "sg", mode="sync"), idle_ttl=100, thread_id=_THREAD)
    assert await fake_redis.hget(store.state_key("s1"), "thread_id") is None
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()


async def test_reverse_index_cleared_on_answer(fake_redis):
    store = InteractionStore("t:")
    await store.add(
        fake_redis, _park_request(store, "i1", "g1"), idle_ttl=86400, continuation_fingerprint="fp-1", thread_id=_THREAD
    )
    claimed = await store.record_answer(
        fake_redis,
        InteractionResponse(interaction_id="i1", answer="yes", answered_by="op", answered_at=datetime.now(UTC)),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    assert claimed is True
    # Answered → dropped from BOTH the reverse index and the expiry index.
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()
    assert await store.due_expiries(fake_redis, datetime.now(UTC) + timedelta(days=1)) == []


async def test_reverse_index_cleared_on_prune(fake_redis):
    store = InteractionStore("t:")
    await store.add(
        fake_redis, _park_request(store, "i1", "g1"), idle_ttl=86400, continuation_fingerprint="fp-1", thread_id=_THREAD
    )
    assert await store.prune_pending(fake_redis, "i1", "g1") == "pruned"
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()


# -- store: thread reverse-index snapshot + reconcile ------------------------


async def test_thread_park_members_snapshots_the_reverse_index(fake_redis):
    store = InteractionStore("t:")
    for iid, group in (("i1", "g1"), ("i2", "g2")):
        await store.add(
            fake_redis,
            _park_request(store, iid, group),
            idle_ttl=86400,
            continuation_fingerprint="fp",
            thread_id=_THREAD,
        )
    await store.add(
        fake_redis,
        _park_request(store, "other", "og"),
        idle_ttl=86400,
        continuation_fingerprint="fp",
        thread_id=_OTHER_THREAD,
    )
    assert set(await store.thread_park_members(fake_redis, _THREAD)) == {"i1", "i2"}
    assert await store.thread_park_members(fake_redis, _OTHER_THREAD) == ["other"]
    # A thread with no parks is an empty snapshot.
    assert await store.thread_park_members(fake_redis, "bridge:chat:nobody") == []


async def test_reconcile_thread_park_members_removes_only_the_snapshot(fake_redis):
    # SREM only the snapshotted members: a park added concurrently (not in the snapshot) keeps its
    # member, so the kill cascade never blind-wipes a newcomer.
    store = InteractionStore("t:")
    await fake_redis.sadd(store.thread_parks_key(_THREAD), "a", "ghost")
    await store.reconcile_thread_park_members(fake_redis, _THREAD, ["a", "ghost"])
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()
    # A fresh member added after the snapshot survives a reconcile of the old snapshot.
    await fake_redis.sadd(store.thread_parks_key(_THREAD), "b")
    await store.reconcile_thread_park_members(fake_redis, _THREAD, ["a"])
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {"b"}
    # A no-op on an empty snapshot.
    await store.reconcile_thread_park_members(fake_redis, _THREAD, [])
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {"b"}


# -- helper: ask captures the bound thread -------------------------------


def _wire(monkeypatch, fake_client_ctx) -> InteractionsSettings:
    settings = InteractionsSettings()
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    return settings


async def test_ask_captures_thread_from_a_tool_route_delivery_address(monkeypatch, fake_redis, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    # A TOOL route target binds the turn's thread on its delivery address.
    completion_token = set_park_completion("deliver_tool_completion", {"route_name": "chat"}, thread_id=_THREAD)
    try:
        with tool_call_frame():
            result = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_park_completion(completion_token)
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)

    assert isinstance(result, SuspendedInteraction)
    store = InteractionStore("interactions:")
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {result.interaction_id}


async def test_ask_captures_thread_from_an_agent_route_delivery_address(monkeypatch, fake_redis, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    # An AGENT route target binds its thread on the delivery address too, and runs inside a bridge
    # turn context. The bridge names a DIFFERENT thread on purpose: only a discriminating value
    # shows the park reads the address, never the bridge.
    park_token = set_park_completion("agent_completion", {"message_id": "m-1"}, thread_id=_THREAD)
    bridge = BridgeTurnContext(
        thread_id=_OTHER_THREAD, route_name="chat", channel="twilio", our_identity="+1", client_address="+15550001111"
    )
    try:
        with tool_call_frame(), bridge_turn_context(bridge):
            result = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_park_completion(park_token)
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)

    assert isinstance(result, SuspendedInteraction)
    store = InteractionStore("interactions:")
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {result.interaction_id}
    assert await fake_redis.smembers(store.thread_parks_key(_OTHER_THREAD)) == set()


async def test_ask_with_no_bound_thread_indexes_nothing(monkeypatch, fake_redis, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        with tool_call_frame():
            result = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)

    assert isinstance(result, SuspendedInteraction)
    store = InteractionStore("interactions:")
    # Still a valid park (expiry-indexed), just not bound to any thread.
    assert await store.get_state(fake_redis, result.interaction_id) is not None
    assert await fake_redis.hget(store.state_key(result.interaction_id), "thread_id") is None
