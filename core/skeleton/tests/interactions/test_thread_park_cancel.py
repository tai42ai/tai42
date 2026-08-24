"""The thread→interaction reverse index and its cascade cancel — Phase 1 of the
"parked-ask orphaned by thread deletion" fix.

An async ``ask_user`` park is stored keyed by interaction id alone, so a conversation
thread delete had no way to reach it and left it orphaned: the expiry reaper later
fired a continuation into a deleted thread (a delivery retry storm) and the channel
correlation stayed muted until the ~24h deadline. This suite proves the reverse index
that closes that gap: a park bound to a thread joins ``thread-parks:{thread_id}``, the
member is dropped wherever the interaction leaves pending (answer / prune), and
``cancel_thread_parks`` tears every park on a thread down via the status-gated
``prune_pending`` (firing NO continuation), leaving a late answer not-found and the
expiry reaper with nothing to fire.
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

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations.turn_context import BridgeTurnContext, bridge_turn_context
from tai42_skeleton.interactions import InteractionStore, ask_user
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings

_THREAD = "bridge:chat:+15550001111"
_OTHER_THREAD = "bridge:chat:+15559990000"


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # Set BEFORE any InteractionsSettings() is built, so the ask_user OFF-gate passes.
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


# -- store: cancel_thread_parks ----------------------------------------------


async def test_cancel_thread_parks_prunes_every_park_and_clears_the_index(fake_redis):
    store = InteractionStore("t:")
    # Two parks on the target thread, one on a DIFFERENT thread that must be untouched.
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

    cancelled = await store.cancel_thread_parks(fake_redis, _THREAD)

    assert set(cancelled) == {"i1", "i2"}
    # Both target-thread parks are gone (state + expiry member + index).
    for iid in ("i1", "i2"):
        assert await store.get_state(fake_redis, iid) is None
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()
    assert await store.due_expiries(fake_redis, datetime.now(UTC) + timedelta(days=1)) == ["other"]
    # The park on the OTHER thread is fully intact.
    assert await store.get_state(fake_redis, "other") is not None
    assert await fake_redis.smembers(store.thread_parks_key(_OTHER_THREAD)) == {"other"}


async def test_cancel_thread_parks_is_idempotent(fake_redis):
    store = InteractionStore("t:")
    # No parks at all: a clean no-op.
    assert await store.cancel_thread_parks(fake_redis, _THREAD) == []
    # One park, cancelled twice: the second run finds the set drained.
    await store.add(
        fake_redis, _park_request(store, "i1", "g1"), idle_ttl=86400, continuation_fingerprint="fp", thread_id=_THREAD
    )
    assert await store.cancel_thread_parks(fake_redis, _THREAD) == ["i1"]
    assert await store.cancel_thread_parks(fake_redis, _THREAD) == []


async def test_cancel_leaves_a_late_answer_not_found_and_the_reaper_firing_nothing(fake_redis):
    store = InteractionStore("t:")
    await store.add(
        fake_redis, _park_request(store, "i1", "g1"), idle_ttl=86400, continuation_fingerprint="fp-1", thread_id=_THREAD
    )
    await store.cancel_thread_parks(fake_redis, _THREAD)

    # A late answer to the cancelled interaction finds no state → claims nothing.
    claimed = await store.record_answer(
        fake_redis,
        InteractionResponse(interaction_id="i1", answer="too late", answered_by="op", answered_at=datetime.now(UTC)),
        group_id="g1",
        reply_ttl=60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=0,
    )
    assert claimed is False
    # The expiry reaper has nothing to fire for it (no continuation into a dead thread).
    assert await store.due_expiries(fake_redis, datetime.now(UTC) + timedelta(days=1)) == []


async def test_cancel_reconciles_an_orphan_index_member(fake_redis):
    # A member whose state already vanished (answered/expired) is reconciled off the index
    # without a prune, never left to strand the set.
    store = InteractionStore("t:")
    await fake_redis.sadd(store.thread_parks_key(_THREAD), "ghost")
    assert await store.cancel_thread_parks(fake_redis, _THREAD) == ["ghost"]
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == set()


async def test_cancel_leaves_a_park_added_concurrently_with_the_cascade(fake_redis, monkeypatch):
    # A park that arrives on the SAME thread DURING the cascade — after the members snapshot,
    # before the index cleanup — must survive with its index member intact so a retry can
    # still cancel it. The cascade must SREM only the members it snapshotted, never blind-
    # DELETE the whole set (which would silently orphan the newcomer — the very bug this
    # feature prevents). Simulated by injecting a new park mid prune-loop.
    store = InteractionStore("t:")
    await store.add(
        fake_redis, _park_request(store, "a", "ga"), idle_ttl=86400, continuation_fingerprint="fp", thread_id=_THREAD
    )

    real_prune = store.prune_pending
    injected = {"done": False}

    async def prune_then_inject(r, interaction_id, group_id):
        result = await real_prune(r, interaction_id, group_id)
        if not injected["done"]:
            injected["done"] = True
            await store.add(
                r, _park_request(store, "b", "gb"), idle_ttl=86400, continuation_fingerprint="fp", thread_id=_THREAD
            )
        return result

    monkeypatch.setattr(store, "prune_pending", prune_then_inject)
    cancelled = await store.cancel_thread_parks(fake_redis, _THREAD)

    # 'a' (snapshotted) is cancelled; the concurrently-added 'b' survives in BOTH state and
    # the reverse index — a blind DELETE would have wiped it, orphaning it.
    assert cancelled == ["a"]
    assert await store.get_state(fake_redis, "a") is None
    assert await store.get_state(fake_redis, "b") is not None
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {"b"}


# -- helper: ask_user captures the bound thread -------------------------------


def _wire(monkeypatch, fake_client_ctx) -> InteractionsSettings:
    settings = InteractionsSettings()
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    return settings


async def test_ask_user_captures_thread_from_tool_park_completion_context(
    monkeypatch, fake_redis, fake_client_ctx
):
    _wire(monkeypatch, fake_client_ctx)
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    # A TOOL/flow route target binds the delivery thread as the park-completion context.
    completion_token = set_park_completion(
        "deliver_tool_completion", {"delivery_thread_id": _THREAD, "route_name": "chat"}
    )
    try:
        result = await ask_user("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_park_completion(completion_token)
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)

    assert isinstance(result, SuspendedInteraction)
    store = InteractionStore("interactions:")
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {result.interaction_id}


async def test_ask_user_captures_thread_from_agent_bridge_turn_context(
    monkeypatch, fake_redis, fake_client_ctx
):
    _wire(monkeypatch, fake_client_ctx)
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    # An AGENT route target runs inside the bridge turn context AND co-sets a park completion
    # with NO context (exactly as _run_agent_turn does — set_park_completion(COMPLETION_TOOL_NAME)
    # carries no delivery_thread_id). So the thread must come from the bridge, and this pins that
    # a stray/empty agent completion context never shadows it.
    park_token = set_park_completion("agent_completion")
    bridge = BridgeTurnContext(
        thread_id=_THREAD, route_name="chat", channel="twilio", our_identity="+1", client_address="+15550001111"
    )
    try:
        with bridge_turn_context(bridge):
            result = await ask_user("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_park_completion(park_token)
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)

    assert isinstance(result, SuspendedInteraction)
    store = InteractionStore("interactions:")
    assert await fake_redis.smembers(store.thread_parks_key(_THREAD)) == {result.interaction_id}


async def test_ask_user_with_no_bound_thread_indexes_nothing(monkeypatch, fake_redis, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        result = await ask_user("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)

    assert isinstance(result, SuspendedInteraction)
    store = InteractionStore("interactions:")
    # Still a valid park (expiry-indexed), just not bound to any thread.
    assert await store.get_state(fake_redis, result.interaction_id) is not None
    assert await fake_redis.hget(store.state_key(result.interaction_id), "thread_id") is None
