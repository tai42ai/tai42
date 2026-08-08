"""The per-target config store — CRUD over the redis surface, the row+index atomic pair,
the orphan-member skip, and the backend-off 501."""

from __future__ import annotations

import pytest
from tai42_contract.conversations import TargetConversationConfig

from tai42_skeleton.conversations import target_config as store_module
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore
from tai42_skeleton.operations.errors import NotSupportedError

from .fake_config_redis import FakeConfigRedis, make_config_client_ctx


@pytest.fixture
def store(monkeypatch) -> ConversationTargetConfigStore:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeConfigRedis()
    monkeypatch.setattr(store_module, "client_ctx", make_config_client_ctx(fake))
    built = ConversationTargetConfigStore(ConversationsSettings())
    built.fake = fake  # type: ignore[attr-defined]
    return built


async def test_upsert_creates_then_replaces(store):
    created = await store.upsert(TargetConversationConfig(target_kind="agent", target_name="concierge"))
    assert created is True
    replaced = await store.upsert(
        TargetConversationConfig(target_kind="agent", target_name="concierge", multichannel=True)
    )
    assert replaced is False
    got = await store.get("agent", "concierge")
    assert got is not None
    assert got.multichannel is True


async def test_get_missing_is_none(store):
    assert await store.get("agent", "nobody") is None


async def test_list_returns_every_config_keyed_by_pair(store):
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="concierge"))
    await store.upsert(
        TargetConversationConfig(target_kind="tool", target_name="lookup", greeting_template="hi {pairing_code}")
    )
    listed = await store.list()
    assert set(listed) == {("agent", "concierge"), ("tool", "lookup")}
    assert listed[("tool", "lookup")].greeting_template == "hi {pairing_code}"


async def test_delete_removes_row_and_index_member(store):
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="concierge"))
    assert await store.delete("agent", "concierge") is True
    assert await store.get("agent", "concierge") is None
    assert await store.list() == {}
    # ``list() == {}`` alone passes even on a leaked index member (it orphan-skips an
    # indexed-but-rowless member), so assert the names set itself no longer carries the member.
    assert await store.fake.smembers(store.settings.target_config_names_key) == set()
    # A second delete of the now-absent key removes nothing.
    assert await store.delete("agent", "concierge") is False


async def test_list_skips_an_indexed_member_with_no_row(store):
    await store.upsert(TargetConversationConfig(target_kind="agent", target_name="concierge"))
    # An index member whose row never landed (or was dropped from under it) is logged and
    # skipped, never surfaced as a half-row.
    store.fake.seed_member(store.settings.target_config_names_key, "tool:ghost")
    listed = await store.list()
    assert set(listed) == {("agent", "concierge")}


async def test_a_target_name_bearing_a_colon_round_trips(store):
    # ``target_name`` sits LAST in the key, so a ``:`` in it is not a separator hazard.
    await store.upsert(TargetConversationConfig(target_kind="tool", target_name="ns:lookup"))
    got = await store.get("tool", "ns:lookup")
    assert got is not None
    assert got.target_name == "ns:lookup"
    assert set(await store.list()) == {("tool", "ns:lookup")}


def test_construction_refuses_without_the_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    with pytest.raises(NotSupportedError):
        ConversationTargetConfigStore(ConversationsSettings())
