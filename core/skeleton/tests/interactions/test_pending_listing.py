"""The read-only ``InteractionStore.list_pending`` admin audit: parked async asks
appear with their per-item fields (``thread_id`` when the state hash carries one,
tolerantly ``None`` otherwise), answered/pruned asks never do, the limit caps the
slice (soonest-expiry first), and an empty index yields ``[]`` — all WITHOUT mutating
the ``pending:expiry`` index.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse

from tai42_skeleton.interactions import InteractionStore


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


def _park(
    store: InteractionStore,
    *,
    iid: str,
    expiry_at: datetime,
    gid: str = "pg",
    question: str = "proceed?",
    channel: str | None = None,
    recipient: str | None = None,
    audience: str | None = None,
) -> InteractionRequest:
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question=question,
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry_at,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=expiry_at,
        channel=channel,
        recipient=recipient,
        audience=audience,
    )


async def test_empty_index_returns_empty_list(fake_redis):
    store = InteractionStore("i:")
    assert await store.list_pending(fake_redis, now=datetime.now(UTC), limit=500) == []


async def test_parked_ask_appears_with_all_fields(fake_redis):
    store = InteractionStore("i:")
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    park = _park(
        store,
        iid="a1",
        gid="g1",
        expiry_at=future,
        question="q1",
        channel="telegram",
        recipient="@ops",
        audience="user-7",
    )
    await store.add(fake_redis, park, idle_ttl=86400, continuation_fingerprint="fp")

    items = await store.list_pending(fake_redis, now=now, limit=500)
    assert len(items) == 1
    item = items[0]
    assert item == {
        "interaction_id": "a1",
        "group_id": "g1",
        "question": "q1",
        "channel": "telegram",
        "recipient": "@ops",
        "audience": "user-7",
        "thread_id": None,  # no cascade-era thread_id on this park
        "expiry_at": future.isoformat(),
        "created_at": park.created_at.isoformat(),
        "mode": "async",
    }


async def test_thread_id_read_tolerantly_when_present(fake_redis):
    # A cascade-era park carries a denormalized ``thread_id`` hash field; the audit
    # surfaces it, and its absence on an older park is tolerated (None).
    store = InteractionStore("i:")
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    await store.add(fake_redis, _park(store, iid="withthread", expiry_at=future), idle_ttl=86400)
    await store.add(fake_redis, _park(store, iid="nothread", expiry_at=future), idle_ttl=86400)
    # Simulate the cascade feature stamping the denormalized field on one park's hash.
    await fake_redis.hset(store.state_key("withthread"), mapping={"thread_id": "T-9"})

    items = {item["interaction_id"]: item for item in await store.list_pending(fake_redis, now=now, limit=500)}
    assert items["withthread"]["thread_id"] == "T-9"
    assert items["nothread"]["thread_id"] is None


async def test_answered_and_pruned_asks_do_not_appear(fake_redis):
    store = InteractionStore("i:")
    now = datetime.now(UTC)
    future = now + timedelta(hours=1)
    await store.add(fake_redis, _park(store, iid="live", gid="gl", expiry_at=future), idle_ttl=86400)
    await store.add(fake_redis, _park(store, iid="answered", gid="ga", expiry_at=future), idle_ttl=86400)
    await store.add(fake_redis, _park(store, iid="pruned", gid="gp", expiry_at=future), idle_ttl=86400)

    # An async park's claim requires the continuation-due timing (it enqueues a durable
    # continuation-due record atomically with the claim).
    await store.record_answer(
        fake_redis,
        InteractionResponse(
            interaction_id="answered", answer="done", answered_by="tester", answered_at=datetime.now(UTC)
        ),
        "ga",
        reply_ttl=60,
        continuation_due_ttl=60,
        continuation_first_attempt_at_ms=int(datetime.now(UTC).timestamp() * 1000),
    )
    await store.prune_pending(fake_redis, "pruned", "gp")

    listed = {item["interaction_id"] for item in await store.list_pending(fake_redis, now=now, limit=500)}
    assert listed == {"live"}


async def test_limit_caps_the_slice_soonest_first(fake_redis):
    store = InteractionStore("i:")
    now = datetime.now(UTC)
    # Three parks with staggered deadlines; the soonest two come back under limit=2.
    for iid, gid, mins in (("soon", "g1", 1), ("mid", "g2", 5), ("late", "g3", 9)):
        park = _park(store, iid=iid, gid=gid, expiry_at=now + timedelta(minutes=mins))
        await store.add(fake_redis, park, idle_ttl=86400)

    items = await store.list_pending(fake_redis, now=now, limit=2)
    assert [item["interaction_id"] for item in items] == ["soon", "mid"]
    # The audit did not mutate the index — a full read still sees all three.
    assert len(await store.list_pending(fake_redis, now=now, limit=500)) == 3


async def test_question_truncated_to_preview_length(fake_redis):
    from tai42_skeleton.interactions.store import _PENDING_QUESTION_PREVIEW_CHARS

    store = InteractionStore("i:")
    now = datetime.now(UTC)
    long_q = "x" * (_PENDING_QUESTION_PREVIEW_CHARS + 50)
    await store.add(
        fake_redis, _park(store, iid="q1", expiry_at=now + timedelta(hours=1), question=long_q), idle_ttl=86400
    )
    items = await store.list_pending(fake_redis, now=now, limit=500)
    assert len(items[0]["question"]) == _PENDING_QUESTION_PREVIEW_CHARS
