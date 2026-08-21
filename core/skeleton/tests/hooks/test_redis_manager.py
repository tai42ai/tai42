"""``RedisHooksManager`` register / list / unregister against the faked redis
hash + pipeline seam.
"""

from __future__ import annotations

from tai42_contract.hooks import HookParams

from tai42_skeleton.hooks.managers import redis_hooks_manager as redis_module
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.settings import HooksSettings

from .conftest import make_client_ctx


def _manager(monkeypatch, fake) -> RedisHooksManager:
    monkeypatch.setattr(redis_module, "client_ctx", make_client_ctx(fake))
    return RedisHooksManager(HooksSettings())


async def test_register_writes_hook_and_topic_map(monkeypatch, fake_redis):
    manager = _manager(monkeypatch, fake_redis)
    assert (
        await manager.register(
            HookParams(
                name="h1",
                topic="events",
                tool="forward",
                execution_key="k-fire",
                execution_key_fingerprint="fp-fire",
                condition=".ok",
            )
        )
        is True
    )

    by_topic = await manager.list_hooks_by_topic("events")
    assert by_topic["h1"].tool == "forward"
    assert await manager.list_hooks_by_topic("empty") == {}


async def test_register_rejects_invalid_jq(monkeypatch, fake_redis):
    import pytest

    manager = _manager(monkeypatch, fake_redis)
    with pytest.raises(ValueError, match="not valid jq"):
        await manager.register(
            HookParams(
                name="bad",
                topic="t",
                tool="noop",
                execution_key="k-fire",
                execution_key_fingerprint="fp-fire",
                condition="( not jq",
            )
        )


async def test_list_hooks_aggregates_across_topics(monkeypatch, fake_redis):
    manager = _manager(monkeypatch, fake_redis)
    await manager.register(
        HookParams(name="a", topic="t1", tool="x", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )
    await manager.register(
        HookParams(name="b", topic="t2", tool="y", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )

    listed = await manager.list_hooks()
    assert set(listed.keys()) == {"a", "b"}
    assert listed["a"].topic == "t1"
    assert listed["b"].topic == "t2"


async def test_list_hooks_empty_when_no_map(monkeypatch, fake_redis):
    manager = _manager(monkeypatch, fake_redis)
    assert await manager.list_hooks() == {}


async def test_unregister_removes_hook_and_returns_true(monkeypatch, fake_redis):
    manager = _manager(monkeypatch, fake_redis)
    await manager.register(
        HookParams(
            name="h1", topic="events", tool="forward", execution_key="k-fire", execution_key_fingerprint="fp-fire"
        )
    )

    assert await manager.unregister("h1") is True
    assert await manager.list_hooks() == {}
    assert await manager.list_hooks_by_topic("events") == {}


async def test_unregister_unknown_returns_false(monkeypatch, fake_redis):
    manager = _manager(monkeypatch, fake_redis)
    assert await manager.unregister("nope") is False


async def test_reregister_under_new_topic_moves_hook(monkeypatch, fake_redis, make_app):
    # Re-registering the same name under a new topic must drop the old topic's
    # entry: the old topic fires nothing, the new topic fires the hook, and
    # unregister still works afterwards.
    app = make_app()
    manager = _manager(monkeypatch, fake_redis)
    await manager.register(
        HookParams(
            name="mv", topic="topic-a", tool="mv_tool", execution_key="k-fire", execution_key_fingerprint="fp-fire"
        )
    )
    await manager.register(
        HookParams(
            name="mv", topic="topic-b", tool="mv_tool", execution_key="k-fire", execution_key_fingerprint="fp-fire"
        )
    )

    await manager.on_event("topic-a", {})
    assert app.tools.runs == []

    await manager.on_event("topic-b", {})
    assert app.tools.runs == [("mv_tool", {})]

    assert await manager.unregister("mv") is True
    assert await manager.list_hooks() == {}
    assert await manager.list_hooks_by_topic("topic-b") == {}


async def test_register_funnels_through_single_eval_leaving_one_topic_entry(monkeypatch, fake_redis):
    # The fake's ``eval`` runs each Lua script synchronously to completion, so this
    # does NOT interleave two registers — real atomicity is Redis's single-threaded
    # Lua guarantee, not exercised here. What this verifies: each register funnels
    # the move through a SINGLE ``eval`` call, and after two moves of the SAME name
    # the end-state invariant holds — the name lives under exactly one topic hash
    # and the name->topic map agrees with it.
    import asyncio

    manager = _manager(monkeypatch, fake_redis)

    # Count eval calls to confirm each register performs its move as one eval.
    eval_calls = 0
    real_eval = fake_redis.eval

    async def counting_eval(*args, **kwargs):
        nonlocal eval_calls
        eval_calls += 1
        return await real_eval(*args, **kwargs)

    monkeypatch.setattr(fake_redis, "eval", counting_eval)

    await manager.register(
        HookParams(name="mv", topic="topic-a", tool="t", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )

    await asyncio.gather(
        manager.register(
            HookParams(
                name="mv", topic="topic-b", tool="t", execution_key="k-fire", execution_key_fingerprint="fp-fire"
            )
        ),
        manager.register(
            HookParams(
                name="mv", topic="topic-c", tool="t", execution_key="k-fire", execution_key_fingerprint="fp-fire"
            )
        ),
    )

    # Three registers, one eval each — the move is funnelled through a single eval.
    assert eval_calls == 3

    topics_holding_mv = [
        key.split("hooks:topic:", 1)[1]
        for key, fields in fake_redis._hashes.items()
        if key.startswith("hooks:topic:") and "mv" in fields
    ]
    assert len(topics_holding_mv) == 1
    # The name->topic map agrees with the single surviving topic hash.
    assert fake_redis._hashes["hooks:name_trigger_map"]["mv"] == topics_holding_mv[0]


async def test_claim_webhook_delivery_first_true_replay_false(monkeypatch, fake_redis):
    # First claim of a (topic, key) wins; a replay of the same pair is refused.
    manager = _manager(monkeypatch, fake_redis)
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is True
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is False
    # A different id (same topic) is a distinct delivery — never dropped.
    assert await manager.claim_webhook_delivery("events", "github:d-2", 300) is True
    # Same id, different topic — distinct door, distinct delivery.
    assert await manager.claim_webhook_delivery("other", "github:d-1", 300) is True


async def test_claim_webhook_delivery_sets_ttl_atomically(monkeypatch, fake_redis):
    # The claim writes its key WITH an expiry in one op: after the TTL lapses the key is
    # gone (never a permanent key), so the same id claims fresh again.
    manager = _manager(monkeypatch, fake_redis)
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is True
    key = manager.settings.webhook_seen_key("events", "github:d-1")
    # The key carries a bounded expiry, not an unbounded (None) one.
    assert fake_redis._strings[key][1] is not None
    fake_redis.advance(301)
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is True


async def test_claim_webhook_delivery_non_positive_ttl_raises(monkeypatch, fake_redis):
    import pytest

    manager = _manager(monkeypatch, fake_redis)
    # Never a claim without a positive bounded TTL.
    with pytest.raises(ValueError, match="positive ttl_seconds"):
        await manager.claim_webhook_delivery("events", "d", 0)


async def test_list_hooks_skips_orphaned_map_entry(monkeypatch, fake_redis):
    # The name->topic map references a hook whose hash entry is gone (e.g. it
    # expired): list_hooks must skip the orphan rather than emit a null hook.
    manager = _manager(monkeypatch, fake_redis)
    await manager.register(
        HookParams(name="live", topic="t", tool="x", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )
    await manager.register(
        HookParams(name="orphan", topic="t", tool="y", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )
    # Drop only the hook hash field, leaving the map entry behind.
    del fake_redis._hashes["hooks:topic:t"]["orphan"]

    listed = await manager.list_hooks()
    assert set(listed.keys()) == {"live"}
