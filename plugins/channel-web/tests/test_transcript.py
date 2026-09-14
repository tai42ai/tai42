"""The transcript stream — the message/media/question/answer appenders, the exact
max-entries trim, the one-pipeline write, and the backlog/tail replay reads."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from tai42_channel_web.store import connection
from tai42_channel_web.store.transcript import (
    append_answered,
    append_media,
    append_message,
    append_question,
    capture_cursor,
    read_backlog_batch,
    read_tail,
)

from .conftest import CALLBACK, IDENTITY, VISITOR_ID, FakeRedis, _deadline

pytestmark = pytest.mark.usefixtures("web_env")

_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"


def _entries(fake: FakeRedis) -> list[tuple[str, dict[str, str]]]:
    return fake.streams.get(_TRANSCRIPT_KEY, [])


def _data(entry: tuple[str, dict[str, str]]) -> dict:
    return json.loads(entry[1]["data"])


async def test_append_message_writes_frame_and_refreshes_ttl(fake_redis: FakeRedis):
    entry_id = await append_message(IDENTITY, VISITOR_ID, "in", "hello", entry_id="msg-7")

    assert entry_id == "msg-7"
    entry = _entries(fake_redis)[0]
    assert entry[1]["event"] == "chat.message"
    payload = _data(entry)
    assert payload["id"] == "msg-7"
    assert payload["direction"] == "in"
    assert payload["text"] == "hello"
    assert "ts" in payload
    assert fake_redis.ttls[_TRANSCRIPT_KEY] == 30 * 86400


async def test_append_message_mints_id_when_none(fake_redis: FakeRedis):
    entry_id = await append_message(IDENTITY, VISITOR_ID, "out", "hi")
    assert entry_id
    assert _data(_entries(fake_redis)[0])["id"] == entry_id


async def test_append_question_carries_widget_fields(fake_redis: FakeRedis):
    timeout_at = _deadline()
    entry_id = await append_question(
        IDENTITY, VISITOR_ID, "int-1", "Which env?", "select", ["staging", "production"], timeout_at
    )
    payload = _data(_entries(fake_redis)[0])
    assert _entries(fake_redis)[0][1]["event"] == "chat.question"
    assert payload["id"] == entry_id
    assert payload["interaction_id"] == "int-1"
    assert payload["answer_format"] == "select"
    assert payload["options"] == ["staging", "production"]
    assert payload["timeout_at"] == timeout_at.astimezone(UTC).isoformat()
    # The callback ticket is not shipped to a widget that has no use for it.
    assert "callback_url" not in payload


async def test_append_question_carries_the_callback_only_when_given(fake_redis: FakeRedis):
    await append_question(IDENTITY, VISITOR_ID, "int-1", "Sign here", "external", None, _deadline(), CALLBACK)
    assert _data(_entries(fake_redis)[0])["callback_url"] == CALLBACK


async def test_append_question_carries_the_form_schema_only_when_given(fake_redis: FakeRedis):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    await append_question(IDENTITY, VISITOR_ID, "int-1", "Your details", "form", None, _deadline(), schema=schema)
    payload = _data(_entries(fake_redis)[0])
    assert payload["schema"] == schema
    # The schema is display-input material for the form widget, never a callback ticket.
    assert "callback_url" not in payload


async def test_append_question_omits_the_schema_when_none(fake_redis: FakeRedis):
    await append_question(IDENTITY, VISITOR_ID, "int-1", "Which env?", "select", ["a", "b"], _deadline())
    assert "schema" not in _data(_entries(fake_redis)[0])


async def test_append_question_carries_form_data_and_pages_only_when_given(fake_redis: FakeRedis):
    schema = {"type": "object", "properties": {"colour": {"type": "string"}}}
    form_data = {"values": {"colour": "r"}, "options": {"colour": [{"value": "r", "label": "Red"}]}}
    pages = [{"title": "Pick", "fields": ["colour"]}]
    await append_question(
        IDENTITY,
        VISITOR_ID,
        "int-1",
        "Your details",
        "form",
        None,
        _deadline(),
        schema=schema,
        form_data=form_data,
        pages=pages,
    )
    payload = _data(_entries(fake_redis)[0])
    assert payload["data"] == form_data
    assert payload["pages"] == pages


async def test_append_question_omits_form_data_and_pages_when_none(fake_redis: FakeRedis):
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    await append_question(IDENTITY, VISITOR_ID, "int-1", "Your details", "form", None, _deadline(), schema=schema)
    payload = _data(_entries(fake_redis)[0])
    assert "data" not in payload
    assert "pages" not in payload


async def test_append_answered_carries_answer(fake_redis: FakeRedis):
    await append_answered(IDENTITY, VISITOR_ID, "int-1", {"choice": "staging"})
    entry = _entries(fake_redis)[0]
    assert entry[1]["event"] == "chat.answered"
    payload = _data(entry)
    assert payload["interaction_id"] == "int-1"
    assert payload["answer"] == {"choice": "staging"}


async def test_append_media_carries_card_fields(fake_redis: FakeRedis):
    media = [{"kind": "image", "url": "https://cdn.example/p.png", "caption": "a pattern"}]
    options = [{"kind": "reply", "text": "Item A"}, {"kind": "link", "label": "Read more", "url": "https://ex/more"}]
    entry_id = await append_media(IDENTITY, VISITOR_ID, "see this", media, options)
    entry = _entries(fake_redis)[0]
    assert entry[1]["event"] == "chat.media"
    payload = _data(entry)
    assert payload["id"] == entry_id
    assert payload["direction"] == "out"
    assert payload["text"] == "see this"
    assert payload["media"] == media
    assert payload["options"] == options


async def test_append_media_carries_sections_header_footer_and_location(fake_redis: FakeRedis):
    # Every rich card field rides its own frame key when present: a sectioned reply list,
    # a media header, a footer line, and a location map-pin.
    sections = [{"title": "Today", "rows": [{"kind": "reply", "text": "09:00"}]}]
    header = {"kind": "image", "url": "https://cdn.example/banner.png", "caption": "Banner"}
    location = {"latitude": 51.5, "longitude": -0.12, "name": "London"}
    await append_media(
        IDENTITY,
        VISITOR_ID,
        "choose",
        None,
        None,
        sections=sections,
        header=header,
        footer="Powered by TAI",
        location=location,
    )
    payload = _data(_entries(fake_redis)[0])
    assert payload["sections"] == sections
    assert payload["header"] == header
    assert payload["footer"] == "Powered by TAI"
    assert payload["location"] == location


async def test_append_media_omits_absent_fields(fake_redis: FakeRedis):
    # No card content: every optional key is omitted, never emitted empty.
    await append_media(IDENTITY, VISITOR_ID, "text only", None, None)
    payload = _data(_entries(fake_redis)[0])
    assert payload["text"] == "text only"
    for key in ("media", "options", "sections", "header", "footer", "location"):
        assert key not in payload


async def test_append_media_replays_through_the_backlog_pager(fake_redis: FakeRedis):
    # A chat.media entry is re-emitted verbatim by the backlog reader — the replay path
    # filters no entry by event type.
    await append_media(IDENTITY, VISITOR_ID, "see this", [{"kind": "image", "url": "https://cdn.example/p.png"}], None)
    async with connection.pooled_redis_ctx() as redis:
        _start, frames = await read_backlog_batch(redis, IDENTITY, VISITOR_ID, "-", "+", 100)
    assert len(frames) == 1
    assert frames[0].startswith("event: chat.media\ndata: ")
    assert '"url": "https://cdn.example/p.png"' in frames[0]


async def test_append_trims_to_max_entries(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    # The cap is EXACT (``MAXLEN`` without ``~``): the settings comment, this test and
    # a production server must all describe the same stream length.
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_TRANSCRIPT_MAX_ENTRIES", "3")
    reset_all_settings()
    for i in range(5):
        await append_message(IDENTITY, VISITOR_ID, "in", f"m{i}")
    texts = [_data(e)["text"] for e in _entries(fake_redis)]
    assert texts == ["m2", "m3", "m4"]


async def test_append_writes_the_entry_and_its_ttl_in_one_pipeline(fake_redis: FakeRedis):
    # XADD + EXPIRE unpipelined would be two round trips on every single frame.
    calls: list[str] = []

    class _CountingPipeline:
        def __init__(self) -> None:
            self.queued: list[str] = []

        def xadd(self, *args: object, **kwargs: object) -> _CountingPipeline:
            self.queued.append("xadd")
            return self

        def expire(self, *args: object, **kwargs: object) -> _CountingPipeline:
            self.queued.append("expire")
            return self

        async def execute(self) -> list[object]:
            calls.extend(self.queued)
            return [None for _ in self.queued]

    monkeypatch_pipeline = _CountingPipeline()
    fake_redis.pipeline = lambda: monkeypatch_pipeline  # type: ignore[method-assign]

    await append_message(IDENTITY, VISITOR_ID, "in", "hello")

    assert calls == ["xadd", "expire"]


async def test_append_message_echoes_the_client_retry_key(fake_redis: FakeRedis):
    # The page draws a bubble optimistically; the echoed key is how the replayed
    # frame is matched to it after a response the browser never got.
    await append_message(IDENTITY, VISITOR_ID, "in", "hi", entry_id="turn-1", client_message_id="abc-123_XY")
    assert _data(_entries(fake_redis)[0])["client_message_id"] == "abc-123_XY"


async def test_append_message_carries_no_key_when_the_sender_sent_none(fake_redis: FakeRedis):
    # An invented key would match no bubble the page ever drew.
    await append_message(IDENTITY, VISITOR_ID, "out", "hi")
    assert "client_message_id" not in _data(_entries(fake_redis)[0])


async def test_capture_cursor_empty_stream_is_zero(fake_redis: FakeRedis):
    async with connection.pooled_redis_ctx() as redis:
        assert await capture_cursor(redis, IDENTITY, VISITOR_ID) == "0-0"


async def test_capture_cursor_is_latest_entry(fake_redis: FakeRedis):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    latest = (await append_message(IDENTITY, VISITOR_ID, "in", "two"), _entries(fake_redis)[-1][0])
    async with connection.pooled_redis_ctx() as redis:
        assert await capture_cursor(redis, IDENTITY, VISITOR_ID) == latest[1]


async def test_read_backlog_returns_frames_and_skips_malformed(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    # A malformed entry (missing event/data) is skipped, never fatal — and the skip is
    # visible at WARNING, like every other recovered-from corruption in this store.
    fake_redis.streams[_TRANSCRIPT_KEY].append(("999-0", {"garbage": "x"}))
    with caplog.at_level("WARNING"):
        async with connection.pooled_redis_ctx() as redis:
            start, frames = await read_backlog_batch(redis, IDENTITY, VISITOR_ID, "-", "+", 100)
    assert start is None
    assert len(frames) == 1
    assert frames[0].startswith("event: chat.message\ndata: ")
    assert any("malformed" in r.message for r in caplog.records)


async def test_read_backlog_pages_the_transcript_by_count(fake_redis: FakeRedis):
    # A whole transcript is never materialized: each page is bounded by COUNT and the
    # returned start is where the next page picks up, with nothing repeated or lost.
    for i in range(5):
        await append_message(IDENTITY, VISITOR_ID, "in", f"m{i}")

    seen: list[str] = []
    start: str | None = "-"
    pages = 0
    async with connection.pooled_redis_ctx() as redis:
        while start is not None:
            start, frames = await read_backlog_batch(redis, IDENTITY, VISITOR_ID, start, "+", 2)
            seen += frames
            pages += 1

    assert pages == 3
    assert [f'"text": "m{i}"' in frame for i, frame in enumerate(seen)] == [True] * 5


async def test_read_backlog_skips_a_malformed_entry_in_a_later_page(
    fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    # The skip must survive paging: a corrupt entry past the first page is dropped
    # exactly like one in it, and the page after it still starts in the right place.
    for i in range(3):
        await append_message(IDENTITY, VISITOR_ID, "in", f"m{i}")
    fake_redis.streams[_TRANSCRIPT_KEY].insert(2, ("2-5", {"garbage": "x"}))

    seen: list[str] = []
    start: str | None = "-"
    with caplog.at_level("WARNING"):
        async with connection.pooled_redis_ctx() as redis:
            while start is not None:
                start, frames = await read_backlog_batch(redis, IDENTITY, VISITOR_ID, start, "+", 2)
                seen += frames

    assert len(seen) == 3
    assert '"text": "m2"' in seen[2]
    assert any("malformed" in r.message and "2-5" in r.message for r in caplog.records)


async def test_read_backlog_stops_at_the_end_bound(fake_redis: FakeRedis):
    # The end bound is the cursor the tail resumes from. Reading past it (``max="+"``)
    # would emit every entry written during a slow replay twice — once here and again
    # from the tail.
    await append_message(IDENTITY, VISITOR_ID, "in", "before")
    async with connection.pooled_redis_ctx() as redis:
        cursor = await capture_cursor(redis, IDENTITY, VISITOR_ID)
        await append_message(IDENTITY, VISITOR_ID, "out", "during the replay")
        start, frames = await read_backlog_batch(redis, IDENTITY, VISITOR_ID, "-", cursor, 100)

    assert start is None
    assert len(frames) == 1
    assert '"text": "before"' in frames[0]


async def test_read_tail_advances_cursor_and_returns_new_frames(fake_redis: FakeRedis):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    async with connection.pooled_redis_ctx() as redis:
        cursor = await capture_cursor(redis, IDENTITY, VISITOR_ID)
        await append_message(IDENTITY, VISITOR_ID, "out", "two")
        new_cursor, frames = await read_tail(redis, IDENTITY, VISITOR_ID, cursor, 1)
    assert len(frames) == 1
    assert '"text": "two"' in frames[0]
    assert new_cursor != cursor


async def test_read_tail_skips_malformed(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    fake_redis.streams[_TRANSCRIPT_KEY] = [("1-0", {"garbage": "x"})]
    with caplog.at_level("DEBUG"):
        async with connection.pooled_redis_ctx() as redis:
            new_cursor, frames = await read_tail(redis, IDENTITY, VISITOR_ID, "0-0", 1)
    assert frames == []
    assert new_cursor == "1-0"
    assert any("malformed" in r.message for r in caplog.records)
