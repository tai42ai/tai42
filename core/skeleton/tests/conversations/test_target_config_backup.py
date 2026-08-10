"""The ``conversation_target_config`` backup section — export/import round-trip, skip vs
overwrite, per-row model rejection, and the backend-off refusals."""

from __future__ import annotations

import pytest
from tai42_contract.conversations import TargetConversationConfig

from tai42_skeleton.conversations import target_config as store_module
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore
from tai42_skeleton.conversations.target_config_backup import export_target_configs, import_target_configs

from .fake_config_redis import FakeConfigRedis, make_config_client_ctx


@pytest.fixture
def store(monkeypatch) -> ConversationTargetConfigStore:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeConfigRedis()
    monkeypatch.setattr(store_module, "client_ctx", make_config_client_ctx(fake))
    return ConversationTargetConfigStore(ConversationsSettings())


async def test_export_import_round_trip(store):
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=True))
    await store.upsert(
        TargetConversationConfig(target_kind="tool", target_name="lookup", greeting_template="hi {pairing_code}")
    )
    exported = await export_target_configs()
    assert {(c["target_kind"], c["target_name"]) for c in exported["target_configs"]} == {
        ("agent", "assistant"),
        ("tool", "lookup"),
    }

    # Wipe and restore from the export.
    for kind, name in (("agent", "assistant"), ("tool", "lookup")):
        await store.delete(kind, name)
    report = await import_target_configs(exported, "skip")
    assert report["created"] == 2
    restored = await store.get("tool", "lookup")
    assert restored is not None
    assert restored.greeting_template == "hi {pairing_code}"


async def test_skip_leaves_an_existing_config_untouched(store):
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=False))
    payload = {"target_configs": [{"target_kind": "agent", "target_name": "assistant", "multichannel": True}]}
    report = await import_target_configs(payload, "skip")
    assert report["skipped_existing"] == 1
    assert report["created"] == 0
    kept = await store.get("agent", "assistant")
    assert kept is not None
    assert kept.multichannel is False


async def test_overwrite_replaces_an_existing_config(store):
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="assistant", multichannel=False))
    payload = {"target_configs": [{"target_kind": "agent", "target_name": "assistant", "multichannel": True}]}
    report = await import_target_configs(payload, "overwrite")
    assert report["updated"] == 1
    updated = await store.get("agent", "assistant")
    assert updated is not None
    assert updated.multichannel is True


async def test_a_duplicate_pair_in_one_payload_under_skip_is_created_once(store):
    # Two rows carry the SAME (target_kind, target_name) in one payload: the first is created,
    # the second is seen as already-existing and skipped, not counted a second create silently
    # overwriting the first. The stored row is the FIRST one.
    payload = {
        "target_configs": [
            {"target_kind": "agent", "target_name": "assistant", "multichannel": True},
            {"target_kind": "agent", "target_name": "assistant", "multichannel": False},
        ]
    }
    report = await import_target_configs(payload, "skip")
    assert report["created"] == 1
    assert report["skipped_existing"] == 1
    stored = await store.get("agent", "assistant")
    assert stored is not None
    assert stored.multichannel is True


async def test_a_malformed_row_is_rejected_per_row(store):
    payload = {
        "target_configs": [
            {"target_kind": "agent", "target_name": "good"},
            {"target_kind": "agent", "target_name": "bad", "greeting_template": "hi {name}"},
        ]
    }
    report = await import_target_configs(payload, "skip")
    assert report["created"] == 1
    assert report["skipped"] == 1
    assert len(report["errors"]) == 1
    assert await store.get("agent", "good") is not None
    assert await store.get("agent", "bad") is None


async def test_a_missing_envelope_key_raises(store):
    with pytest.raises(ValueError, match="missing the required 'target_configs' key"):
        await import_target_configs({}, "skip")


async def test_export_is_empty_without_the_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    assert await export_target_configs() == {"target_configs": []}


async def test_import_with_rows_refuses_without_the_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    payload = {"target_configs": [{"target_kind": "agent", "target_name": "assistant"}]}
    with pytest.raises(RuntimeError, match="redis conversations backend"):
        await import_target_configs(payload, "skip")


async def test_import_empty_is_a_noop_without_the_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    report = await import_target_configs({"target_configs": []}, "skip")
    assert report["created"] == 0
    assert report["errors"] == []
