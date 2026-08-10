"""Session registrations, the transcript stream, and pending-question records —
register/resolve/drop, append, reserve, peek, claim, restore, backlog/tail reads,
and the fail-closed redis guard."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_channel_web import store
from tai42_channel_web.store import (
    EntryCode,
    QuestionRecord,
    SessionRecordError,
    SessionRegistration,
    append_answered,
    append_message,
    append_question,
    capture_cursor,
    check_entry_code,
    claim_question,
    drop_session,
    entry_attempt_allowed,
    get_entry_code,
    is_gate_enabled,
    list_entry_codes,
    mint_entry_code,
    peek_question,
    read_backlog_batch,
    read_tail,
    register_session,
    release_question,
    reserve_question,
    resolve_session,
    restore_question,
    revoke_entry_code,
    set_gate,
    update_session_params,
)
from tests.conftest import CALLBACK, IDENTITY, OTHER_IDENTITY, SESSION_TOKEN, VISITOR_ID, FakeRedis

pytestmark = pytest.mark.usefixtures("web_env")

_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"
_QUESTION_KEY = "channel:web:question:int-1"
_SESSION_KEY = f"channel:web:session:{SESSION_TOKEN}"


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _entries(fake: FakeRedis) -> list[tuple[str, dict[str, str]]]:
    return fake.streams.get(_TRANSCRIPT_KEY, [])


def _data(entry: tuple[str, dict[str, str]]) -> dict:
    return json.loads(entry[1]["data"])


# -- session registrations ------------------------------------------------------


async def test_register_then_resolve_returns_the_address_and_its_route(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})

    assert json.loads(fake_redis.store[_SESSION_KEY])["visitor_id"] == VISITOR_ID
    # The route the session was minted on rides the registration: it is what binds
    # this token to one conversation surface.
    assert json.loads(fake_redis.store[_SESSION_KEY])["identity"] == IDENTITY
    assert await resolve_session(SESSION_TOKEN) == SessionRegistration(visitor_id=VISITOR_ID, identity=IDENTITY)


async def test_a_fresh_mint_only_gets_the_short_pending_ttl(fake_redis: FakeRedis):
    # Nothing has come back with this cookie yet: an anonymous GET loop must leave
    # minute-lived keys behind, not one 30-day registration per request.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    assert fake_redis.ttls[_SESSION_KEY] == 600


async def test_resolving_promotes_a_pending_registration_to_the_full_ttl(fake_redis: FakeRedis):
    # The cookie came back, so this is a real visitor — from here the registration
    # lives as long as any other session.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})

    assert await resolve_session(SESSION_TOKEN) is not None
    assert fake_redis.ttls[_SESSION_KEY] == 30 * 86400


async def test_resolve_refreshes_the_registration_ttl(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    fake_redis.ttls[_SESSION_KEY] = 5
    monkeypatch.setenv("CHANNEL_WEB_SESSION_TTL_SECONDS", "600")
    reset_all_settings()

    assert await resolve_session(SESSION_TOKEN) is not None
    assert fake_redis.ttls[_SESSION_KEY] == 600


async def test_resolve_reads_and_expires_in_one_round_trip(fake_redis: FakeRedis):
    # GETEX, not GET+EXPIRE: every door that touches a cookie resolves a session, so
    # the second round trip would be paid on every request.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    fake_redis.events.clear()

    await resolve_session(SESSION_TOKEN)

    assert fake_redis.events == [("redis_getex", _SESSION_KEY)]


async def test_resolve_unregistered_token_is_none(fake_redis: FakeRedis):
    # The invented-cookie case: a shape-valid token nobody registered is no session,
    # so it can never become a conversation address.
    assert await resolve_session(SESSION_TOKEN) is None


@pytest.mark.parametrize(
    ("stored", "missing"),
    [
        ({"created_at": "now", "identity": IDENTITY, "params": {}}, "visitor_id"),
        ({"created_at": "now", "visitor_id": VISITOR_ID, "params": {}}, "identity"),
        # No old-format tolerance: a record predating link params (no ``params`` key)
        # fails loud and the visitor re-mints, rather than being silently defaulted.
        ({"created_at": "now", "visitor_id": VISITOR_ID, "identity": IDENTITY}, "params"),
        ({"visitor_id": VISITOR_ID, "identity": IDENTITY, "params": "x"}, "params"),
        ({"visitor_id": VISITOR_ID, "identity": IDENTITY, "params": {"k": 1}}, "params"),
    ],
)
async def test_resolve_malformed_registration_raises(fake_redis: FakeRedis, stored: dict, missing: str):
    # Every half is load-bearing — the address a door speaks for, the route it may
    # speak on, and the captured params map — so a record missing or mis-typing any is
    # a loud fault, never a session.
    fake_redis.store[_SESSION_KEY] = json.dumps(stored)
    with pytest.raises(SessionRecordError, match=missing):
        await resolve_session(SESSION_TOKEN)


async def test_register_captures_and_resolves_params(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "spring", "n": "3"})
    stored = json.loads(fake_redis.store[_SESSION_KEY])
    assert stored["params"] == {"ref": "spring", "n": "3"}
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.params == {"ref": "spring", "n": "3"}


async def test_a_fresh_mint_without_params_still_carries_the_empty_map(fake_redis: FakeRedis):
    # The key is ALWAYS present — the strict decode requires it, so there is no
    # absent-key shape to tolerate.
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    assert json.loads(fake_redis.store[_SESSION_KEY])["params"] == {}


async def test_update_session_params_rewrites_at_the_full_ttl(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "old"})
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    await update_session_params(SESSION_TOKEN, registration, {"ref": "new"})
    stored = json.loads(fake_redis.store[_SESSION_KEY])
    # Same token, same visitor id, new params — and the cookie came back, so a real
    # visitor gets the FULL session TTL.
    assert stored["visitor_id"] == VISITOR_ID
    assert stored["identity"] == IDENTITY
    assert stored["params"] == {"ref": "new"}
    assert fake_redis.ttls[_SESSION_KEY] == 30 * 86400


async def test_drop_session_unregisters_the_token(fake_redis: FakeRedis):
    await register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {})
    await drop_session(SESSION_TOKEN)
    assert await resolve_session(SESSION_TOKEN) is None


# -- transcript + question records ----------------------------------------------


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


async def test_append_answered_carries_answer(fake_redis: FakeRedis):
    await append_answered(IDENTITY, VISITOR_ID, "int-1", {"choice": "staging"})
    entry = _entries(fake_redis)[0]
    assert entry[1]["event"] == "chat.answered"
    payload = _data(entry)
    assert payload["interaction_id"] == "int-1"
    assert payload["answer"] == {"choice": "staging"}


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


async def test_reserve_sets_record_with_remaining_ttl(fake_redis: FakeRedis):
    before = datetime.now(UTC)
    timeout_at = _deadline(120)
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=timeout_at)
    await reserve_question("int-1", record)
    after = datetime.now(UTC)

    stored = json.loads(fake_redis.store[_QUESTION_KEY])
    assert stored == {
        "callback_url": CALLBACK,
        "identity": IDENTITY,
        "address": VISITOR_ID,
        "timeout_at": timeout_at.astimezone(UTC).isoformat(),
        "restores": 0,
    }
    ttl = fake_redis.ttls[_QUESTION_KEY]
    assert math.ceil((timeout_at - after).total_seconds()) <= ttl <= math.ceil((timeout_at - before).total_seconds())


async def test_reserve_past_deadline_raises_and_stores_nothing(fake_redis: FakeRedis):
    from tai42_contract.channels import ChannelDeliveryError

    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline(-1))
    with pytest.raises(ChannelDeliveryError, match="already passed"):
        await reserve_question("int-1", record)
    assert _QUESTION_KEY not in fake_redis.store


async def test_peek_reads_without_claiming(fake_redis: FakeRedis):
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    await reserve_question("int-1", record)

    assert await peek_question("int-1") == record
    # Still claimable: the ownership check must not consume the record.
    assert await claim_question("int-1") == record


async def test_peek_unknown_question_is_none(fake_redis: FakeRedis):
    assert await peek_question("int-nope") is None


async def test_claim_is_atomic_getdel(fake_redis: FakeRedis):
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    await reserve_question("int-1", record)

    assert await claim_question("int-1") == record
    assert await claim_question("int-1") is None


async def test_release_drops_the_reservation(fake_redis: FakeRedis):
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    await reserve_question("int-1", record)
    await release_question("int-1")
    assert await claim_question("int-1") is None


async def test_restore_uses_remaining_ttl(fake_redis: FakeRedis):
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline(600))
    await reserve_question("int-1", record)
    claimed = await claim_question("int-1")
    assert claimed is not None

    await restore_question("int-1", claimed, 5)

    assert fake_redis.ttls[_QUESTION_KEY] <= 600
    assert await claim_question("int-1") == replace(claimed, restores=1)


async def test_restore_past_deadline_stores_nothing(fake_redis: FakeRedis):
    stale = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline(-1))
    assert await restore_question("int-1", stale, 5) is False
    assert _QUESTION_KEY not in fake_redis.store


async def test_restore_never_clobbers_a_new_reservation(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    await reserve_question("int-1", record)
    claimed = await claim_question("int-1")
    assert claimed is not None
    new_record = QuestionRecord(
        callback_url="https://app.example/next", identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline()
    )
    await reserve_question("int-1", new_record)

    with caplog.at_level("ERROR"):
        assert await restore_question("int-1", claimed, 5) is False

    still = await claim_question("int-1")
    assert still == new_record
    assert any("could not restore" in r.message for r in caplog.records)


async def test_restore_counts_itself_on_the_record(fake_redis: FakeRedis):
    # The count rides the record, so it expires exactly with the question and needs
    # no second key of its own.
    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    await reserve_question("int-1", record)
    claimed = await claim_question("int-1")
    assert claimed is not None
    assert claimed.restores == 0

    assert await restore_question("int-1", claimed, 5) is True

    again = await claim_question("int-1")
    assert again is not None
    assert again.restores == 1
    assert again.callback_url == CALLBACK


async def test_restore_is_refused_once_the_cap_is_spent(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    # An answer the callback door keeps refusing is an unauthenticated loop over that
    # door's own rate limit (keyed on this server's egress IP, shared by every
    # channel): past the cap the record stays dropped, loudly.
    record = QuestionRecord(
        callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline(), restores=2
    )
    with caplog.at_level("ERROR"):
        assert await restore_question("int-1", record, 2) is False

    assert _QUESTION_KEY not in fake_redis.store
    assert any("already been refused" in r.message for r in caplog.records)


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
    async with store.pooled_redis_ctx() as redis:
        assert await capture_cursor(redis, IDENTITY, VISITOR_ID) == "0-0"


async def test_capture_cursor_is_latest_entry(fake_redis: FakeRedis):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    latest = (await append_message(IDENTITY, VISITOR_ID, "in", "two"), _entries(fake_redis)[-1][0])
    async with store.pooled_redis_ctx() as redis:
        assert await capture_cursor(redis, IDENTITY, VISITOR_ID) == latest[1]


async def test_read_backlog_returns_frames_and_skips_malformed(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    # A malformed entry (missing event/data) is skipped, never fatal — and the skip is
    # visible at WARNING, like every other recovered-from corruption in this store.
    fake_redis.streams[_TRANSCRIPT_KEY].append(("999-0", {"garbage": "x"}))
    with caplog.at_level("WARNING"):
        async with store.pooled_redis_ctx() as redis:
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
    async with store.pooled_redis_ctx() as redis:
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
        async with store.pooled_redis_ctx() as redis:
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
    async with store.pooled_redis_ctx() as redis:
        cursor = await capture_cursor(redis, IDENTITY, VISITOR_ID)
        await append_message(IDENTITY, VISITOR_ID, "out", "during the replay")
        start, frames = await read_backlog_batch(redis, IDENTITY, VISITOR_ID, "-", cursor, 100)

    assert start is None
    assert len(frames) == 1
    assert '"text": "before"' in frames[0]


async def test_read_tail_advances_cursor_and_returns_new_frames(fake_redis: FakeRedis):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    async with store.pooled_redis_ctx() as redis:
        cursor = await capture_cursor(redis, IDENTITY, VISITOR_ID)
        await append_message(IDENTITY, VISITOR_ID, "out", "two")
        new_cursor, frames = await read_tail(redis, IDENTITY, VISITOR_ID, cursor, 1)
    assert len(frames) == 1
    assert '"text": "two"' in frames[0]
    assert new_cursor != cursor


async def test_read_tail_skips_malformed(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    fake_redis.streams[_TRANSCRIPT_KEY] = [("1-0", {"garbage": "x"})]
    with caplog.at_level("DEBUG"):
        async with store.pooled_redis_ctx() as redis:
            new_cursor, frames = await read_tail(redis, IDENTITY, VISITOR_ID, "0-0", 1)
    assert frames == []
    assert new_cursor == "1-0"
    assert any("malformed" in r.message for r in caplog.records)


async def test_every_store_op_raises_when_redis_url_unset(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_WEB_REDIS_URL")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()

    record = QuestionRecord(callback_url=CALLBACK, identity=IDENTITY, address=VISITOR_ID, timeout_at=_deadline())
    for call in (
        register_session(SESSION_TOKEN, VISITOR_ID, IDENTITY, {}),
        resolve_session(SESSION_TOKEN),
        drop_session(SESSION_TOKEN),
        append_message(IDENTITY, VISITOR_ID, "in", "x"),
        append_question(IDENTITY, VISITOR_ID, "int-1", "q", "text", None, _deadline()),
        append_answered(IDENTITY, VISITOR_ID, "int-1", "a"),
        reserve_question("int-1", record),
        release_question("int-1"),
        claim_question("int-1"),
        restore_question("int-1", record, 5),
    ):
        with pytest.raises(ValueError, match="CHANNEL_WEB_REDIS_URL"):
            await call


def test_redis_guard_passes_when_set():
    assert store._redis_settings().redis_url == "redis://test/0"


def test_redis_url_falls_back_to_the_default_namespace(monkeypatch: pytest.MonkeyPatch):
    # The store connection falls back to the shared TAI_DEFAULT_REDIS_URL when the
    # channel's own url is unset; the guard fires only when NEITHER is set.
    monkeypatch.delenv("CHANNEL_WEB_REDIS_URL")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared/1")
    reset_all_settings()
    assert store._redis_settings().redis_url == "redis://shared/1"


def test_tail_connection_strips_the_socket_read_timeout(stub_app, fake_redis: FakeRedis):
    # The keepalive XREAD blocks legitimately; a blanket read timeout would kill it.
    store.tail_redis_ctx()
    kwargs = stub_app.clients.ctx_kwargs[-1]
    assert kwargs["settings"].socket_timeout is None
    assert kwargs["fresh"] is True


# -- entry gate + codes ---------------------------------------------------------

_GATE_KEY = f"channel:web:entry_gate:{IDENTITY}"


async def test_gate_flag_is_explicit(fake_redis: FakeRedis):
    # An ungated route reads False; the flag is set explicitly and read back True.
    assert await is_gate_enabled(IDENTITY) is False
    await set_gate(IDENTITY, True)
    assert fake_redis.store[_GATE_KEY] == "1"
    assert await is_gate_enabled(IDENTITY) is True
    await set_gate(IDENTITY, False)
    assert _GATE_KEY not in fake_redis.store
    assert await is_gate_enabled(IDENTITY) is False


async def test_mint_stores_only_the_hash_and_returns_the_raw_code_once(fake_redis: FakeRedis):
    raw_code, code_id = await mint_entry_code(IDENTITY, "spring launch", None)
    assert code_id == hashlib.sha256(raw_code.encode()).hexdigest()
    key = f"channel:web:entry_code:{IDENTITY}:{code_id}"
    # The raw code is never at rest — only its hash keys the record.
    assert raw_code not in json.dumps(fake_redis.store)
    stored = json.loads(fake_redis.store[key])
    assert stored["label"] == "spring launch"
    assert stored["expires_at"] is None
    # No expiry -> no TTL.
    assert key not in fake_redis.ttls


async def test_mint_with_an_expiry_sets_a_ttl(fake_redis: FakeRedis):
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    _, code_id = await mint_entry_code(IDENTITY, None, expires_at)
    key = f"channel:web:entry_code:{IDENTITY}:{code_id}"
    assert json.loads(fake_redis.store[key])["expires_at"] == expires_at.isoformat()
    assert 3500 <= fake_redis.ttls[key] <= 3600


async def test_mint_refuses_a_past_expiry(fake_redis: FakeRedis):
    with pytest.raises(ValueError, match="not in the future"):
        await mint_entry_code(IDENTITY, None, datetime.now(UTC) - timedelta(seconds=1))


async def test_check_entry_code_is_liveness_by_hash(fake_redis: FakeRedis):
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    assert await check_entry_code(IDENTITY, raw_code) is True
    assert await check_entry_code(IDENTITY, "not-the-code") is False
    # A live code is scoped to its own route — the same raw value is unknown elsewhere.
    assert await check_entry_code(OTHER_IDENTITY, raw_code) is False


async def test_get_and_list_expose_metadata_never_the_raw_code(fake_redis: FakeRedis):
    raw_code, code_id = await mint_entry_code(IDENTITY, "launch", None)
    fetched = await get_entry_code(IDENTITY, code_id)
    assert fetched is not None
    assert fetched == EntryCode(code_id=code_id, label="launch", created_at=fetched.created_at, expires_at=None)
    listed = await list_entry_codes(IDENTITY)
    assert [code.code_id for code in listed] == [code_id]
    assert raw_code not in json.dumps([code.__dict__ for code in listed])


async def test_revoke_kills_the_code_and_reports_whether_it_existed(fake_redis: FakeRedis):
    raw_code, code_id = await mint_entry_code(IDENTITY, None, None)
    assert await revoke_entry_code(IDENTITY, code_id) is True
    assert await check_entry_code(IDENTITY, raw_code) is False
    # A second revoke of the same id finds nothing.
    assert await revoke_entry_code(IDENTITY, code_id) is False


async def test_deleting_every_code_leaves_the_gate_closed(fake_redis: FakeRedis):
    # The gate flag is EXPLICIT: revoking the last code does not silently reopen the
    # route — it stays gated, and now unreachable until a code is minted.
    await set_gate(IDENTITY, True)
    _, code_id = await mint_entry_code(IDENTITY, None, None)
    await revoke_entry_code(IDENTITY, code_id)
    assert await is_gate_enabled(IDENTITY) is True
    assert await list_entry_codes(IDENTITY) == []


async def test_list_escapes_a_glob_metachar_identity(fake_redis: FakeRedis):
    # ``_clean_identity`` admits Redis glob metacharacters; an unescaped ``*`` in the
    # SCAN pattern would match every sibling identity's keys. The escape keeps the
    # segment literal, so a ``*`` identity matches only its OWN codes.
    _, star_code = await mint_entry_code("*", None, None)
    await mint_entry_code("site-alpha", None, None)
    await mint_entry_code("site-beta", None, None)
    listed = await list_entry_codes("*")
    assert [code.code_id for code in listed] == [star_code]


async def test_throttle_allows_up_to_the_cap_then_refuses(fake_redis: FakeRedis):
    bucket = "net-1.2.3.4"
    key = f"channel:web:entry_throttle:{bucket}"
    # The default cap is 10 per window.
    allowed = [await entry_attempt_allowed(bucket) for _ in range(11)]
    assert allowed == [True] * 10 + [False]
    # The window is set once, on the first attempt.
    assert fake_redis.ttls[key] == 300


async def test_throttle_is_per_bucket(fake_redis: FakeRedis):
    for _ in range(10):
        await entry_attempt_allowed("net-a")
    assert await entry_attempt_allowed("net-a") is False
    # A different bucket has its own budget.
    assert await entry_attempt_allowed("net-b") is True
