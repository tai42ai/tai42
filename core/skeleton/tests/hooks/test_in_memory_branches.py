"""Edge branches of the ``InMemoryHooksManager`` registry: removing one hook from
a topic that still holds another leaves the topic bucket in place, and
re-registering a name under a new topic moves it out of the old topic's bucket.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.hooks import HookParams

from tai42_skeleton.hooks.managers import in_memory_hooks_manager as in_memory_module
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.settings import HooksSettings


async def test_unregister_one_of_two_keeps_topic_bucket():
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(
        HookParams(name="a", topic="t", tool="x", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )
    await manager.register(
        HookParams(name="b", topic="t", tool="y", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )

    assert await manager.unregister("a") is True
    # The topic bucket survives because "b" is still registered under it.
    assert set((await manager.list_hooks_by_topic("t")).keys()) == {"b"}

    assert await manager.unregister("b") is True
    assert await manager.list_hooks_by_topic("t") == {}


async def test_reregister_under_new_topic_moves_hook(make_app):
    # Re-registering the same name under a new topic must drop the old topic's
    # bucket entry: the old topic fires nothing, the new topic fires the hook,
    # and unregister still works afterwards.
    app = make_app()
    manager = InMemoryHooksManager(HooksSettings())
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


async def test_claim_webhook_delivery_first_true_replay_false():
    manager = InMemoryHooksManager(HooksSettings())
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is True
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is False
    # Distinct id and same-id-other-topic are distinct deliveries — never dropped.
    assert await manager.claim_webhook_delivery("events", "github:d-2", 300) is True
    assert await manager.claim_webhook_delivery("other", "github:d-1", 300) is True


async def test_claim_webhook_delivery_expires_and_reclaims(monkeypatch: pytest.MonkeyPatch):
    # After the TTL lapses (a monotonic-clock tick), the same id claims fresh again and
    # the lapsed key is purged rather than kept forever.
    manager = InMemoryHooksManager(HooksSettings())
    clock = {"now": 1000.0}
    monkeypatch.setattr(in_memory_module.time, "monotonic", lambda: clock["now"])
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is True
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is False
    clock["now"] += 301
    assert await manager.claim_webhook_delivery("events", "github:d-1", 300) is True
    # The lapsed key did not accumulate: only the live claim remains.
    assert list(manager._webhook_seen) == [manager.settings.webhook_seen_key("events", "github:d-1")]


async def test_claim_webhook_delivery_evicts_only_expired_prefix(monkeypatch: pytest.MonkeyPatch):
    # The expiry heap pops only the already-lapsed prefix: with staggered TTLs, advancing
    # the clock past the SHORTEST window drops that id alone; longer-lived ids (whether
    # claimed before or after it) survive without being scanned or re-inserted.
    manager = InMemoryHooksManager(HooksSettings())
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "tai42_skeleton.hooks.managers.in_memory_hooks_manager.time",
        SimpleNamespace(monotonic=lambda: clock["now"]),
    )

    assert await manager.claim_webhook_delivery("events", "short", 10) is True  # expiry 1010
    assert await manager.claim_webhook_delivery("events", "long", 1000) is True  # expiry 2000
    assert await manager.claim_webhook_delivery("events", "mid", 100) is True  # expiry 1100

    # Past the short window only: "short" has lapsed, "mid" and "long" have not.
    clock["now"] = 1050.0
    assert await manager.claim_webhook_delivery("events", "fresh", 10) is True

    seen = set(manager._webhook_seen)
    key = manager.settings.webhook_seen_key
    assert key("events", "short") not in seen
    assert seen == {
        key("events", "long"),
        key("events", "mid"),
        key("events", "fresh"),
    }
    # "short" stays refused-as-gone; a re-claim now succeeds (it was evicted, not kept).
    assert await manager.claim_webhook_delivery("events", "mid", 100) is False


async def test_claim_webhook_delivery_non_positive_ttl_raises():
    manager = InMemoryHooksManager(HooksSettings())
    with pytest.raises(ValueError, match="positive ttl_seconds"):
        await manager.claim_webhook_delivery("events", "d", 0)
