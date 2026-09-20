"""A person's stored locale: seeded from the channel at first contact, left untouched by a
later turn, and overridable (or clearable) through the operator write door."""

from __future__ import annotations

import pytest

from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations.persons import ConversationPersonStore, PairingTarget
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx
from .test_persons import _addr

pytestmark = pytest.mark.asyncio

_TARGET = PairingTarget(target_kind="agent", target_name="assistant")


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


def _store(monkeypatch, fake: FakeRecordRedis) -> ConversationPersonStore:
    monkeypatch.setattr(persons_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationPersonStore(ConversationsSettings())


async def test_first_contact_seeds_locale_and_later_turn_keeps_it(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    person, created = await store.ensure_provisional(_TARGET, _addr("+15550002222"), locale="he")
    assert created is True
    assert person.locale == "he"
    # A later turn carrying a different channel hint must not clobber the stored locale.
    again, created_again = await store.ensure_provisional(_TARGET, _addr("+15550002222"), locale="en")
    assert created_again is False
    assert again.locale == "he"


async def test_set_locale_overrides_and_clears(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    person, _ = await store.ensure_provisional(_TARGET, _addr("+15550003333"), locale="he")
    updated = await store.set_locale(person.person_id, "en-US")
    assert updated is not None
    assert updated.locale == "en-US"
    cleared = await store.set_locale(person.person_id, None)
    assert cleared is not None
    assert cleared.locale is None


async def test_set_locale_missing_person_returns_none(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    assert await store.set_locale("nope", "he") is None
