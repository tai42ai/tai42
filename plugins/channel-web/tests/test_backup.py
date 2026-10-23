"""The ``channel_web_words`` backup section: export every identity's words, import them back, refuse a
document that is not a words archive, and idempotent registration."""

from __future__ import annotations

import types

import pytest

from tai42_channel_web import backup
from tai42_channel_web.backup import WordsArchiveError, export_words, import_words
from tai42_channel_web.store.words import get_words, set_words
from tai42_channel_web.words import WebWords

from .conftest import IDENTITY, OTHER_IDENTITY, FakeRedis

_ONE = {"transport": {"busy": "Hold on a moment."}}
_TWO = {"page": {"not_a_navigation": {"heading": "Open the link", "text": "Open the chat link directly."}}}


async def test_export_carries_every_identity_words(web_env, fake_redis: FakeRedis):
    await set_words(IDENTITY, WebWords.model_validate(_ONE))
    await set_words(OTHER_IDENTITY, WebWords.model_validate(_TWO))
    assert await export_words() == {"version": 1, "words": {IDENTITY: _ONE, OTHER_IDENTITY: _TWO}}


async def test_export_is_empty_when_the_store_is_off(no_web_env):
    assert await export_words() == {"version": 1, "words": {}}


async def test_import_restores_every_identity_words(web_env, fake_redis: FakeRedis):
    await set_words(IDENTITY, WebWords.model_validate(_TWO))
    report = await import_words({"version": 1, "words": {IDENTITY: _ONE, OTHER_IDENTITY: _TWO}})
    assert (report.created, report.updated, report.errors) == (1, 1, [])
    assert await get_words(IDENTITY) == WebWords.model_validate(_ONE)
    assert await get_words(OTHER_IDENTITY) == WebWords.model_validate(_TWO)


async def test_an_export_imports_back_unchanged(web_env, fake_redis: FakeRedis):
    await set_words(IDENTITY, WebWords.model_validate(_ONE))
    document = await export_words()
    fake_redis.store.clear()
    await import_words(document)
    assert await export_words() == document


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "words": {IDENTITY: {"transport": {"busy": " "}}}},
        {"version": 1, "words": {IDENTITY: {"transport": {"no_such_sentence": "x"}}}},
        {"version": 1, "words": {IDENTITY: {}}},
        {"version": 1, "words": {IDENTITY: {"page": {"link_invalid": {}}}}},
        {"version": 1, "words": {"site:alpha": _ONE}},
        {"version": 1, "words": ["not", "a", "map"]},
        {"version": 1},
        {"version": 2, "words": {}},
        {"version": True, "words": {}},
        {"words": {}},
        ["not", "a", "document"],
    ],
)
async def test_a_document_that_is_not_a_valid_archive_is_refused_before_any_write(
    web_env, fake_redis: FakeRedis, payload
):
    # A valid entry beside the broken one is not written either: the whole document is refused.
    if isinstance(payload, dict) and isinstance(payload.get("words"), dict):
        payload = {**payload, "words": {OTHER_IDENTITY: _TWO, **payload["words"]}}
    with pytest.raises(WordsArchiveError):
        await import_words(payload)
    assert fake_redis.store == {}


async def test_import_with_words_refuses_when_the_store_is_off(no_web_env):
    with pytest.raises(RuntimeError, match="store is not configured"):
        await import_words({"version": 1, "words": {IDENTITY: _ONE}})


async def test_an_empty_import_with_the_store_off_writes_nothing(no_web_env):
    report = await import_words({"version": 1, "words": {}})
    assert (report.created, report.updated) == (0, 0)


def test_the_section_registers_once_and_not_as_a_secret(stub_app, monkeypatch):
    class _Backup:
        def __init__(self) -> None:
            self.registered: list[tuple] = []

        def sections(self):
            return [types.SimpleNamespace(name=name) for (name, *_rest) in self.registered]

        def register_section(self, name, exporter, importer, *, secret=False):
            self.registered.append((name, exporter, importer, secret))

    fake = _Backup()
    monkeypatch.setattr(stub_app, "backup", fake, raising=False)
    backup.register_words_backup_section()
    assert fake.registered == [("channel_web_words", export_words, import_words, False)]
    backup.register_words_backup_section()
    assert len(fake.registered) == 1


def test_the_routes_package_arms_the_section_on_startup(stub_app):
    import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)

    assert backup.register_words_backup_section in stub_app.lifecycle.startup_handlers
