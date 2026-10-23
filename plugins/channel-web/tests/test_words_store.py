"""The per-identity words store: set / get / clear one identity's words, list every identity's, and a
corrupt stored record refused loudly."""

from __future__ import annotations

import json

import pytest

from tai42_channel_web.store.words import WordsRecordError, clear_words, get_words, list_words, set_words
from tai42_channel_web.words import WebWords

from .conftest import IDENTITY, OTHER_IDENTITY, FakeRedis

pytestmark = pytest.mark.usefixtures("web_env")


def _words(heading: str) -> WebWords:
    return WebWords.model_validate(
        {"page": {"link_invalid": {"heading": heading}}, "transport": {"busy": "Hold on a moment."}}
    )


async def test_set_then_get_returns_the_identity_words(fake_redis: FakeRedis):
    await set_words(IDENTITY, _words("Link not valid"))
    assert await get_words(IDENTITY) == _words("Link not valid")
    assert await get_words(OTHER_IDENTITY) is None


async def test_words_are_stored_one_key_per_identity_without_unset_members(fake_redis: FakeRedis):
    await set_words(IDENTITY, _words("Link not valid"))
    stored = json.loads(fake_redis.store[f"channel:web:words:{IDENTITY}"])
    assert stored == {
        "page": {"link_invalid": {"heading": "Link not valid"}},
        "transport": {"busy": "Hold on a moment."},
    }


async def test_clear_removes_only_that_identity(fake_redis: FakeRedis):
    await set_words(IDENTITY, _words("one"))
    await set_words(OTHER_IDENTITY, _words("two"))
    assert await clear_words(IDENTITY) is True
    assert await get_words(IDENTITY) is None
    assert await get_words(OTHER_IDENTITY) == _words("two")
    assert await clear_words(IDENTITY) is False


async def test_list_returns_every_identity_verbatim_glob_characters_included(fake_redis: FakeRedis):
    # An identity may carry the Redis glob metacharacters; the list keys each entry by the exact
    # identity it was stored under, never a pattern match of a sibling.
    for identity in ("site*", "site?", "site[a]", "site"):
        await set_words(identity, _words(identity))
    fake_redis.store["channel:web:entry_gate:site"] = "1"
    listed = await list_words()
    assert listed == {identity: _words(identity) for identity in ("site*", "site?", "site[a]", "site")}


async def test_a_corrupt_stored_record_is_refused_loudly(fake_redis: FakeRedis):
    fake_redis.store[f"channel:web:words:{IDENTITY}"] = json.dumps({"transport": {"busy": "   "}})
    with pytest.raises(WordsRecordError, match=IDENTITY):
        await get_words(IDENTITY)
    with pytest.raises(WordsRecordError, match=IDENTITY):
        await list_words()
