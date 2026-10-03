"""The per-thread owed first-contact greeting (keyspace 9) TTL.

A greeting carrying a ``{pairing_code}`` is bounded by the code's live window, so it is never
delivered after its code has died. A greeting with NO code carries nothing that expires, so it
takes the owed-greeting horizon instead of being dropped at the (short) pair-code lifetime.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


def _store(monkeypatch, fake: FakeRecordRedis) -> ConversationRecordStore:
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRecordStore(ConversationsSettings())


async def test_pair_code_greeting_is_bounded_by_the_code_lifetime(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    key = settings.owed_greeting_key("t1")

    await store.record_owed_greeting("t1", "Welcome, code {code}", carries_pair_code=True)

    # A greeting that carries a live code never outlives the window its code is live for.
    assert fake.ttl_ms[key] == settings.pair_code_ttl_seconds * 1000


async def test_no_code_greeting_outlives_the_pair_code_lifetime(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    key = settings.owed_greeting_key("t2")

    await store.record_owed_greeting("t2", "Welcome!", carries_pair_code=False)

    # A code-less greeting has nothing that expires, so it takes the owed-greeting horizon and is
    # not dropped at the (shorter) pair-code lifetime.
    assert fake.ttl_ms[key] == settings.owed_greeting_ttl_seconds * 1000
    assert settings.owed_greeting_ttl_seconds > settings.pair_code_ttl_seconds
