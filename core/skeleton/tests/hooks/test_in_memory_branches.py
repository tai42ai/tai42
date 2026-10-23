"""Edge branches of the ``InMemoryHooksManager`` registry: removing one hook from
a topic that still holds another leaves the topic bucket in place, and
re-registering a name under a new topic moves it out of the old topic's bucket, and an
accept's webhook replay claim is held per topic for its window.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.hooks import HookParams

from tai42_skeleton.hooks.managers import in_memory_hooks_manager as in_memory_module
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.runs.models import ReplayClaim
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


async def test_reregister_under_new_topic_moves_hook(hook_runs, make_app):
    # Re-registering the same name under a new topic must drop the old topic's
    # bucket entry: the old topic fires nothing, the new topic fires the hook,
    # and unregister still works afterwards.
    app = make_app()
    manager = InMemoryHooksManager(HooksSettings())
    hook_runs.use(manager)
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
    await hook_runs.drain()
    assert app.tools.runs == []

    await manager.on_event("topic-b", {})
    await hook_runs.drain()
    assert app.tools.runs == [("mv_tool", {})]

    assert await manager.unregister("mv") is True
    assert await manager.list_hooks() == {}
    assert await manager.list_hooks_by_topic("topic-b") == {}


async def _accept(manager: InMemoryHooksManager, topic: str, key: str, ttl: int) -> str:
    result = await manager.on_event(topic, {}, replay_claim=ReplayClaim(key, ttl))
    return result.status


async def test_replay_claim_accepts_first_and_answers_a_replay_already_seen():
    manager = InMemoryHooksManager(HooksSettings())
    assert await _accept(manager, "events", "github:d-1", 300) == "accepted"
    assert await _accept(manager, "events", "github:d-1", 300) == "already_seen"
    # Distinct id and same-id-other-topic are distinct deliveries — never dropped.
    assert await _accept(manager, "events", "github:d-2", 300) == "accepted"
    assert await _accept(manager, "other", "github:d-1", 300) == "accepted"


async def test_an_already_seen_delivery_keeps_no_runs():
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(
        HookParams(name="a", topic="events", tool="x", execution_key="k-fire", execution_key_fingerprint="fp-fire")
    )
    first = await manager.on_event("events", {}, replay_claim=ReplayClaim("d-1", 300))
    replay = await manager.on_event("events", {}, replay_claim=ReplayClaim("d-1", 300))
    assert first.status == "accepted"
    assert len(first.run_ids) == 1
    assert replay.status == "already_seen"
    assert replay.run_ids == ()
    page = await manager.list_hook_runs(None, 50, None)
    assert [run.id for run in page.items] == list(first.run_ids)


async def test_replay_claim_expires_and_reclaims(monkeypatch: pytest.MonkeyPatch):
    # After the TTL lapses (a monotonic-clock tick), the same id is accepted fresh again and
    # the lapsed key is purged rather than kept forever.
    manager = InMemoryHooksManager(HooksSettings())
    clock = {"now": 1000.0}
    monkeypatch.setattr(in_memory_module.time, "monotonic", lambda: clock["now"])
    assert await _accept(manager, "events", "github:d-1", 300) == "accepted"
    assert await _accept(manager, "events", "github:d-1", 300) == "already_seen"
    clock["now"] += 301
    assert await _accept(manager, "events", "github:d-1", 300) == "accepted"
    # The lapsed key did not accumulate: only the live claim remains.
    assert list(manager._webhook_seen) == [manager.settings.webhook_seen_key("events", "github:d-1")]


async def test_replay_claim_evicts_only_expired_prefix(monkeypatch: pytest.MonkeyPatch):
    # The expiry heap pops only the already-lapsed prefix: with staggered TTLs, advancing
    # the clock past the SHORTEST window drops that id alone; longer-lived ids (whether
    # claimed before or after it) survive without being scanned or re-inserted.
    manager = InMemoryHooksManager(HooksSettings())
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "tai42_skeleton.hooks.managers.in_memory_hooks_manager.time",
        SimpleNamespace(monotonic=lambda: clock["now"]),
    )

    assert await _accept(manager, "events", "short", 10) == "accepted"  # expiry 1010
    assert await _accept(manager, "events", "long", 1000) == "accepted"  # expiry 2000
    assert await _accept(manager, "events", "mid", 100) == "accepted"  # expiry 1100

    # Past the short window only: "short" has lapsed, "mid" and "long" have not.
    clock["now"] = 1050.0
    assert await _accept(manager, "events", "fresh", 10) == "accepted"

    seen = set(manager._webhook_seen)
    key = manager.settings.webhook_seen_key
    assert key("events", "short") not in seen
    assert seen == {
        key("events", "long"),
        key("events", "mid"),
        key("events", "fresh"),
    }
    # "mid" is still live, so its replay is answered already seen.
    assert await _accept(manager, "events", "mid", 100) == "already_seen"


def test_replay_claim_non_positive_ttl_raises():
    with pytest.raises(ValueError, match="positive ttl_seconds"):
        ReplayClaim("d", 0)
