"""The async ``ask_user`` park in the helper and store: the helper returns a
``SuspendedInteraction`` immediately (never blocks) naming the park's resume owner,
stamps the generic continuation (tool + identity + fingerprint + expiry) onto the
persisted question, and refuses an async ask with no resuming driver, no execution identity, no
``expiry_at``, or an ``expiry_at`` on a sync ask. Plus the per-interaction expiry
index the reaper keys on — populated for an async park, empty for a sync question, and
the re-park horizon notice a CHAINED completion binding (and only a chained one) receives
so a caller suspended on this run can move its inherited deadline with it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.interactions import (
    PARK_COMPLETION_REPARKED,
    AnswerFormat,
    InteractionRequest,
    SuspendedInteraction,
    chained_park_context,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
)

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import InteractionStore, ask_user
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.helper import InteractionTimeoutError
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
    # The sentinel names the park's resume OWNER — the same continuation stamped onto the
    # stored question — so a caller can tell a park it may adopt from a nested run's.
    assert result.resume_owner == "resume_tool"

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


# --- the chained caller's re-park horizon notice -------------------------------------------


@pytest.fixture
def repark_fires(monkeypatch):
    """Capture every tool the async-park path fires, faking ONLY the tool-registry seam."""
    fired: list[tuple[str, dict]] = []

    async def _fake_run_tool(tool, arguments):
        fired.append((tool, arguments))
        return {"ok": True}

    monkeypatch.setattr(helper_module, "tai42_app", SimpleNamespace(tools=SimpleNamespace(run_tool=_fake_run_tool)))
    return fired


async def test_a_park_under_a_chained_binding_notifies_the_new_horizon(
    monkeypatch, fake_redis, fake_client_ctx, driver, repark_fires
):
    _wire(monkeypatch, fake_client_ctx)
    expiry = datetime.now(UTC) + timedelta(hours=3)
    completion = set_park_completion(
        "deliver_chained_park",
        chained_park_context(
            "tai42:chained-park:k1", ("deliver_tool_completion", {"delivery_thread_id": "bridge:r:a"})
        ),
    )
    try:
        result = await ask_user("proceed?", mode="async", expiry_at=expiry)
    finally:
        reset_park_completion(completion)
    assert isinstance(result, SuspendedInteraction)
    # A run that re-parks moves its chained caller's inherited horizon with it: one notice,
    # carrying the chained key, the NEW deadline, and the non-terminal status.
    assert len(repark_fires) == 1
    tool, payload = repark_fires[0]
    assert tool == "deliver_chained_park"
    assert payload["chain_token"] == "tai42:chained-park:k1"
    assert payload["expiry_at"] == expiry.isoformat()
    assert payload["status"] == PARK_COMPLETION_REPARKED
    # The park itself is persisted exactly as any other — the notice is beside it, never
    # instead of it.
    store = InteractionStore("interactions:")
    assert await store.get_state(fake_redis, result.interaction_id) is not None


async def test_a_park_under_a_plain_delivery_binding_notifies_nothing(
    monkeypatch, fake_redis, fake_client_ctx, driver, repark_fires
):
    _wire(monkeypatch, fake_client_ctx)
    # The conversation door's own binding is not a chain: its delivery tool has no horizon to
    # refresh, so it is never fired with a notice it cannot answer.
    completion = set_park_completion("deliver_agent_completion", {"thread_id": "bridge:r:a"})
    try:
        await ask_user("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_park_completion(completion)
    assert repark_fires == []


async def test_a_sync_ask_notifies_nothing(monkeypatch, fake_redis, fake_client_ctx, driver, repark_fires):
    settings = _wire(monkeypatch, fake_client_ctx)
    # Only a PARK moves a chained caller's horizon; a sync question never parks at all.
    completion = set_park_completion(
        "deliver_chained_park", chained_park_context("tai42:chained-park:k1", (None, None))
    )
    try:
        with pytest.raises(InteractionTimeoutError):
            await ask_user("proceed?", timeout=0.01)
    finally:
        reset_park_completion(completion)
    assert settings is not None
    assert repark_fires == []


async def test_a_failing_notice_never_fails_the_park(monkeypatch, fake_redis, fake_client_ctx, driver, caplog):
    _wire(monkeypatch, fake_client_ctx)

    async def _boom(tool, arguments):
        raise RuntimeError("delivery tool is down")

    monkeypatch.setattr(helper_module, "tai42_app", SimpleNamespace(tools=SimpleNamespace(run_tool=_boom)))
    completion = set_park_completion(
        "deliver_chained_park", chained_park_context("tai42:chained-park:k1", (None, None))
    )
    try:
        # The notice refreshes a horizon, it never carries an answer: a persisted park must not
        # be turned into a failed ask by a notifier that is down. The caller simply keeps the
        # horizon it already had, and the failure is announced.
        result = await ask_user("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    finally:
        reset_park_completion(completion)
    assert isinstance(result, SuspendedInteraction)
    assert "re-park horizon notice" in caplog.text
