"""The async ``ask_user`` park in the helper and store: the helper returns a
``SuspendedInteraction`` immediately (never blocks), stamps the generic
continuation (tool + identity + fingerprint + expiry) onto the persisted question,
and refuses an async ask with no resuming driver, no execution identity, no
``expiry_at``, or an ``expiry_at`` on a sync ask. Plus the per-interaction expiry
index the reaper keys on — populated for an async park, empty for a sync question.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore, ask_user
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


def _wire(monkeypatch, fake_client_ctx, **settings_kw) -> InteractionsSettings:
    settings = InteractionsSettings(**settings_kw)
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    return settings


@pytest.fixture
def driver():
    # A bound resuming driver: the resume continuation tool + the execution identity
    # the continuation is later rebound as. Both are reset after the test.
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    yield
    reset_execution_identity(id_token)
    reset_resume_continuation_tool(tool_token)


async def test_async_returns_suspended_without_blocking(monkeypatch, fake_redis, fake_client_ctx, driver):
    _wire(monkeypatch, fake_client_ctx)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    result = await ask_user("proceed?", mode="async", expiry_at=expiry)
    assert isinstance(result, SuspendedInteraction)
    assert result.expiry_at == expiry

    store = InteractionStore("interactions:")
    state = await store.get_state(fake_redis, result.interaction_id)
    assert state is not None
    assert state.status == "pending"
    assert state.request.mode == "async"
    assert state.request.continuation_tool == "resume_tool"
    assert state.request.continuation_identity == "svc-key"
    assert state.request.expiry_at == expiry
    assert await store.continuation_fingerprint(fake_redis, result.interaction_id) == "fp-1"
    # Indexed for the reaper by its expiry.
    assert await store.due_expiries(fake_redis, expiry + timedelta(seconds=1)) == [result.interaction_id]


async def test_async_far_future_expiry_is_expiry_indexed(monkeypatch, fake_redis, fake_client_ctx, driver):
    _wire(monkeypatch, fake_client_ctx)
    # A park whose expiry runs far beyond the idle horizon is still indexed by that
    # expiry, so the reaper fires its continuation once the deadline passes.
    expiry = datetime.now(UTC) + timedelta(days=365)
    result = await ask_user("proceed?", mode="async", expiry_at=expiry)
    assert isinstance(result, SuspendedInteraction)
    assert result.expiry_at == expiry
    store = InteractionStore("interactions:")
    assert await store.due_expiries(fake_redis, expiry + timedelta(seconds=1)) == [result.interaction_id]
    # Not yet due before its deadline.
    assert await store.due_expiries(fake_redis, datetime.now(UTC)) == []


async def test_async_without_expiry_at_raises(monkeypatch, fake_client_ctx, driver):
    # A resuming driver + execution identity are bound, so the ONLY thing missing is
    # the deadline: an async park with no ``expiry_at`` is refused up front.
    _wire(monkeypatch, fake_client_ctx)
    with pytest.raises(ValueError, match="async mode requires expiry_at"):
        await ask_user("q", mode="async")


async def test_async_without_driver_raises(monkeypatch, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    with pytest.raises(RuntimeError, match="resuming driver"):
        await ask_user("q", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))


async def test_async_without_execution_identity_raises(monkeypatch, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    token = set_resume_continuation_tool("resume_tool")
    try:
        with pytest.raises(RuntimeError, match="bound execution identity"):
            await ask_user("q", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_resume_continuation_tool(token)


async def test_expiry_at_forbidden_for_sync(monkeypatch, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    with pytest.raises(ValueError, match="expiry_at is only valid"):
        await ask_user("q", expiry_at=datetime.now(UTC) + timedelta(hours=1))


async def test_timeout_and_expiry_are_mutually_exclusive(monkeypatch, fake_client_ctx):
    _wire(monkeypatch, fake_client_ctx)
    with pytest.raises(ValueError, match="mutually exclusive"):
        await ask_user("q", mode="async", timeout=5, expiry_at=datetime.now(UTC) + timedelta(hours=1))


def _sync_req(store: InteractionStore) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id="s1",
        group_id="sg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key("s1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )


async def test_sync_question_stores_no_continuation_and_is_not_indexed(fake_redis):
    # A sync question carries no continuation fingerprint and never joins the expiry
    # index — the reaper never sees it.
    store = InteractionStore("t:")
    await store.add(fake_redis, _sync_req(store), idle_ttl=100)
    assert await store.continuation_fingerprint(fake_redis, "s1") is None
    assert await store.due_expiries(fake_redis, datetime.now(UTC) + timedelta(days=1)) == []


async def test_async_park_past_expiry_is_not_sync_pruned_in_pending(fake_redis):
    # An async park read via ``pending()`` after its ``expiry_at`` has passed but before
    # the next reaper pass is NEVER sync-pruned — pruning would drop its continuation on
    # the floor. It stays pending and expiry-indexed for the reaper to resolve.
    store = InteractionStore("t:")
    now = datetime.now(UTC)
    past = now - timedelta(seconds=1)
    req = InteractionRequest(
        interaction_id="ap1",
        group_id="apg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key("ap1"),
        created_at=past - timedelta(seconds=10),
        timeout_at=past,  # async: timeout_at == expiry_at, already passed
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=past,
    )
    await store.add(fake_redis, req, idle_ttl=86400, continuation_fingerprint="fp-1")
    assert [r.interaction_id for r in await store.pending(fake_redis)] == ["ap1"]
    state = await store.get_state(fake_redis, "ap1")
    assert state is not None
    assert state.status == "pending"  # not pruned
    assert await store.due_expiries(fake_redis, datetime.now(UTC)) == ["ap1"]
