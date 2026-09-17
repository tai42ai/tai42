"""The answer/record store — inbound dedupe, the exactly-once delivery claim, the
delivery-state transitions, out-of-band receipt ingestion, the reverse index and the
re-drive scan — against the faked redis hash + string + Lua seam."""

from __future__ import annotations

import time

import pytest
from tai42_contract.conversations import DeliveryReceipt

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


def _store(monkeypatch, fake: FakeRecordRedis) -> ConversationRecordStore:
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRecordStore(ConversationsSettings())


def _record(message_id: str = "m1", door: str = "channel", **over) -> ConversationRecord:
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": "line",
        "door": door,
        "thread_id": f"bridge:line:{message_id}",
        "client_address": "+15550002222",
        "channel": "twilio" if door == "channel" else None,
        "our_identity": "+15550001111" if door == "channel" else None,
        "callback_url": "https://cb.example/x" if door == "api" else None,
        "origin": "client",
        "inbound_text": f"ask {message_id}",
        "caller_principal": "alice" if door == "api" else None,
        "answer_status": "answered",
        "answer": "hello there",
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(over)
    return ConversationRecord(**fields)  # type: ignore[arg-type]


def _intake(message_id: str = "m1", **over) -> ConversationRecord:
    """A pre-turn intake record — the shape ``accept`` persists before it claims."""
    return _record(
        message_id,
        delivery_status=DeliveryStatus.ACCEPTED,
        answer_status=None,
        answer=None,
        provider_message_id="PID1",
        **over,
    )


def test_conversation_record_silent_rejects_answer_text():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="a silent record carries no answer text"):
        _record(answer_status="silent", answer="leaked")


# -- the successor_id / overlap-outcome model rule ----------------------------


def _channel_merged(
    message_id: str = "m1", *, status: DeliveryStatus = DeliveryStatus.MERGED, successor_id: str | None = "lead"
) -> ConversationRecord:
    """A channel-door overlap terminal: the outcome rides ``delivery_status``, ``answer_status`` is None."""
    return _record(
        message_id,
        delivery_status=status,
        answer_status=None,
        answer=None,
        successor_id=successor_id,
    )


def _api_merged(
    message_id: str = "m1", *, answer_status: str = "merged", successor_id: str | None = "lead"
) -> ConversationRecord:
    """An API-door overlap outcome: it rides ``pending_delivery`` carrying the ``answer_status`` marker."""
    return _record(
        message_id,
        door="api",
        delivery_status=DeliveryStatus.PENDING_DELIVERY,
        answer_status=answer_status,
        answer=None,
        successor_id=successor_id,
    )


@pytest.mark.parametrize("status", [DeliveryStatus.MERGED, DeliveryStatus.SUPERSEDED])
def test_a_channel_overlap_terminal_carries_a_successor_and_no_answer_status(status):
    record = _channel_merged(status=status, successor_id="the-lead")
    assert record.delivery_status is status
    # The outcome lives in the delivery status; answer_status is None, the channel-silent split.
    assert record.answer_status is None
    assert record.successor_id == "the-lead"


@pytest.mark.parametrize("answer_status", ["merged", "superseded"])
def test_an_api_overlap_marker_rides_pending_delivery_with_its_successor(answer_status):
    record = _api_merged(answer_status=answer_status, successor_id="the-lead")
    assert record.delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert record.answer_status == answer_status
    assert record.successor_id == "the-lead"
    # The api marker delivers as a ConversationAnswer that carries the successor pointer.
    answer = record.answer_payload()
    assert answer.status == answer_status
    assert answer.successor_id == "the-lead"
    assert answer.answer is None


@pytest.mark.parametrize("status", [DeliveryStatus.MERGED, DeliveryStatus.SUPERSEDED])
def test_a_channel_overlap_terminal_requires_a_non_blank_successor(status):
    from pydantic import ValidationError

    for missing in (None, "  "):
        with pytest.raises(ValidationError, match="names its successor turn in a non-blank successor_id"):
            _channel_merged(status=status, successor_id=missing)


@pytest.mark.parametrize("answer_status", ["merged", "superseded"])
def test_an_api_overlap_marker_requires_a_non_blank_successor(answer_status):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="names its successor turn in a non-blank successor_id"):
        _api_merged(answer_status=answer_status, successor_id=None)


def test_a_non_overlap_record_forbids_a_successor():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="only a merged/superseded record carries a successor_id"):
        _record(answer_status="answered", answer="hi", successor_id="nope")
    with pytest.raises(ValidationError, match="only a merged/superseded record carries a successor_id"):
        _record(answer_status="silent", answer=None, successor_id="nope")


# -- the guarded channel-door overlap transitions -----------------------------


@pytest.mark.parametrize(
    ("status", "transition"),
    [(DeliveryStatus.MERGED, "merge_record"), (DeliveryStatus.SUPERSEDED, "supersede_record")],
)
async def test_an_overlap_terminal_transitions_only_from_intake(monkeypatch, status, transition):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    await store.create_record(_intake("m1"), intake_token="worker-1")

    terminal = _channel_merged("m1", status=status, successor_id="lead")
    assert await getattr(store, transition)(terminal) == 1
    record = await store.get_record("m1")
    assert record is not None
    assert record.delivery_status is status
    assert record.successor_id == "lead"
    assert record.answer_status is None
    # A racing re-drive or a second decision finds the record already gone from intake.
    assert await getattr(store, transition)(terminal) == 0


@pytest.mark.parametrize("transition", ["merge_record", "supersede_record"])
async def test_an_overlap_terminal_reports_a_missing_record(monkeypatch, transition):
    store = _store(monkeypatch, FakeRecordRedis())
    status = DeliveryStatus.MERGED if transition == "merge_record" else DeliveryStatus.SUPERSEDED
    assert await getattr(store, transition)(_channel_merged("gone", status=status)) == -1


async def test_merge_record_refuses_a_record_in_the_wrong_state(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    with pytest.raises(ValueError, match="merged write expects a merged record"):
        await store.merge_record(_channel_merged("m1", status=DeliveryStatus.SUPERSEDED))
    with pytest.raises(ValueError, match="superseded write expects a superseded record"):
        await store.supersede_record(_channel_merged("m1", status=DeliveryStatus.MERGED))


# -- accepted_after: the thread read the overlap gather and the pending seam share

_THREAD = "bridge:line:t"


def _thread_intake(message_id: str, created_at: float, **over) -> ConversationRecord:
    """An ``accepted`` participant message on the shared test thread, created at ``created_at``."""
    return _intake(message_id, thread_id=_THREAD, created_at=created_at, **over)


async def test_accepted_after_returns_later_accepted_messages_in_order(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    await store.create_record(_thread_intake("lead", 100.0), intake_token="w")
    await store.create_record(_thread_intake("f1", 101.0), intake_token="w")
    await store.create_record(_thread_intake("f2", 102.0), intake_token="w")

    followers = await store.accepted_after("line", _THREAD, 100.0)
    assert [record.message_id for record in followers] == ["f1", "f2"]
    # The boundary is EXCLUSIVE: the lead at exactly ``created_at`` is never its own follower.
    assert "lead" not in {record.message_id for record in followers}


async def test_accepted_after_skips_a_follower_that_left_accepted(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    await store.create_record(_thread_intake("lead", 100.0), intake_token="w")
    await store.create_record(_thread_intake("f1", 101.0), intake_token="w")
    await store.create_record(_thread_intake("f2", 102.0), intake_token="w")
    # f1's turn completed — it has left intake, so it is no longer pending.
    assert await store.complete_turn(_record("f1", thread_id=_THREAD, created_at=101.0, answer="done")) == 1

    assert [record.message_id for record in await store.accepted_after("line", _THREAD, 100.0)] == ["f2"]


async def test_accepted_after_excludes_events_and_honours_the_kind_and_origin(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    await store.create_record(_thread_intake("lead", 100.0), intake_token="w")
    await store.create_record(_thread_intake("msg", 101.0), intake_token="w")
    await store.create_record(
        _thread_intake("evt", 102.0, inbound_kind="event", inbound_event={"kind": "x"}, inbound_text=""),
        intake_token="w",
    )

    # An event turn is never a merge/pending candidate — the message read excludes it by kind.
    assert [r.message_id for r in await store.accepted_after("line", _THREAD, 100.0)] == ["msg"]
    # The event is exactly what the event-kind read returns.
    assert [r.message_id for r in await store.accepted_after("line", _THREAD, 100.0, kind="event")] == ["evt"]
    # Every accepted record is a client turn, so the operator-origin read is empty.
    assert await store.accepted_after("line", _THREAD, 100.0, origin="operator") == []


async def test_accepted_after_is_bounded_by_the_thread_fifo_depth(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_THREAD_QUEUE_DEPTH", "3")
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    for index in range(6):
        await store.create_record(_thread_intake(f"f{index}", 100.0 + index), intake_token="w")

    followers = await store.accepted_after("line", _THREAD, 100.0)
    # At most ``thread_queue_depth`` rows are read back, oldest-of-the-window first.
    assert [record.message_id for record in followers] == ["f1", "f2", "f3"]


async def test_accepted_after_of_an_unknown_thread_is_empty(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    assert await store.accepted_after("line", "bridge:line:missing", 0.0) == []


def test_in_memory_backend_refuses(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    from tai42_skeleton.operations.errors import NotSupportedError

    with pytest.raises(NotSupportedError):
        ConversationRecordStore(ConversationsSettings())


async def test_claim_inbound_is_idempotent(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    first = await store.claim_inbound("twilio", "PID1", "msg-a")
    assert first == "msg-a"  # fresh claim keeps the caller's id
    # A provider redelivery of the same (channel, provider_message_id) returns the FIRST id.
    again = await store.claim_inbound("twilio", "PID1", "msg-b")
    assert again == "msg-a"
    # A different provider id on the same channel is independent.
    other = await store.claim_inbound("twilio", "PID2", "msg-c")
    assert other == "msg-c"


async def test_get_inbound_owner_reads_without_claiming(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    # The fast-path read takes nothing: an unclaimed pair stays unclaimed, so a later
    # claim by a real accept still wins it.
    assert await store.get_inbound_owner("twilio", "PID1") is None
    assert await store.get_inbound_owner("twilio", "PID1") is None
    assert await store.claim_inbound("twilio", "PID1", "msg-a") == "msg-a"
    assert await store.get_inbound_owner("twilio", "PID1") == "msg-a"


async def test_complete_turn_is_guarded_on_the_intake_state(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_intake("m1"), intake_token="worker-1")
    completed = _record("m1", answer="the answer")

    assert await store.complete_turn(completed) == 1
    got = await store.get_record("m1")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert got.answer == "the answer"
    assert got.answer_status == "answered"
    # A second writer (a late turn racing the re-drive that already resolved the record)
    # is refused rather than overwriting the outcome the client was given.
    assert await store.complete_turn(_record("m1", answer="a different answer")) == 0
    got = await store.get_record("m1")
    assert got is not None
    assert got.answer == "the answer"
    # A record that no longer exists answers -1.
    assert await store.complete_turn(_record("gone", answer="x")) == -1


async def test_complete_turn_refuses_a_record_that_is_not_pending_delivery(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    with pytest.raises(ValueError, match="pending_delivery"):
        await store.complete_turn(_intake("m1"))


async def test_claim_delivery_refuses_an_intake_record(monkeypatch):
    # An intake record carries no answer, so the delivery machine must never take it.
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_intake("m1"), intake_token="worker-1")
    assert await store.claim_delivery("m1", time.time(), "tok", 120) == -2


async def test_delete_record_removes_an_abandoned_intake_record(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    record = _intake("m1")
    await store.create_record(record, intake_token="worker-1")
    assert await store.delete_record(record) is True
    assert await store.get_record("m1") is None
    assert await store.delete_record(record) is False


async def test_a_shed_record_is_created_terminal_with_the_retention_ttl(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    shed = _record(
        "m1",
        delivery_status=DeliveryStatus.SHED,
        answer_status=None,
        answer=None,
        error="over the rate cap",
    )
    await store.create_record(shed)

    got = await store.get_record("m1")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.SHED
    assert got.answer is None
    # It is terminal at birth: it carries the retention TTL, and no transition moves it.
    assert fake.ttl_ms[settings.record_key("m1")] == settings.answer_retention_ttl_seconds * 1000
    assert await store.claim_delivery("m1", time.time(), "tok", 120) == 0
    assert await store.mark_delivered("m1", [], 1, time.time(), "tok") == -2
    assert await store.mark_failed("m1", 1, time.time(), "tok") == -2


async def test_an_intake_record_never_expires(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    await store.create_record(_intake("m1"), intake_token="worker-1")
    assert ConversationsSettings().record_key("m1") not in fake.ttl_ms


async def test_create_and_get_round_trip(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    record = _record(answer="hi", outbound_message_ids=[], attempts=0)
    await store.create_record(record)
    got = await store.get_record("m1")
    assert got is not None
    assert got.answer == "hi"
    assert got.delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert got.door == "channel"
    assert got.our_identity == "+15550001111"
    assert await store.get_record("nope") is None


async def test_answer_parts_round_trip(monkeypatch):
    """An ordered multi-message / rich answer survives the store round-trip: the ``answer_parts``
    part models (message + media) persist as JSON in the content blob and reload intact."""
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    store = _store(monkeypatch, FakeRecordRedis())
    parts = [
        AnswerPart(message="hi"),
        AnswerPart(message="the picture", media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")]),
    ]
    record = _record(answer="hi\n\nthe picture", answer_parts=parts)
    await store.create_record(record)

    got = await store.get_record("m1")
    assert got is not None
    assert got.answer_parts is not None
    assert [p.message for p in got.answer_parts] == ["hi", "the picture"]
    assert got.answer_parts[1].media is not None
    assert got.answer_parts[1].media[0].url == "https://cdn.example/i.png"
    # The api-door payload carries the parts additively; ``answer`` is unchanged.
    payload = got.answer_payload()
    assert payload.answer == "hi\n\nthe picture"
    assert payload.parts is not None
    assert [p.message for p in payload.parts] == ["hi", "the picture"]


async def test_all_media_answer_round_trip_and_empty_answer_consumers(monkeypatch):
    """An ALL-MEDIA answer (every part media-only) stores ``answer=""`` with the parts, survives
    the store round-trip, and each consumer of ``answer`` handles the empty text deliberately: the
    api-door payload carries ``answer=""`` with the parts, the caller view publishes ``answer=""``
    and the parts, and a text search finds nothing (no text to match) without raising."""
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    from tai42_skeleton.conversations.record_keys import _record_matches

    store = _store(monkeypatch, FakeRecordRedis())
    parts = [
        AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.png")]),
        AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.png")]),
    ]
    record = _record(answer="", answer_parts=parts)
    await store.create_record(record)

    got = await store.get_record("m1")
    assert got is not None
    assert got.answer == ""
    assert got.answer_parts is not None
    assert [p.media[0].url for p in got.answer_parts if p.media is not None] == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.png",
    ]
    # api-door payload: answer is the empty string, the media rides parts.
    payload = got.answer_payload()
    assert payload.answer == ""
    assert payload.parts is not None
    assert len(payload.parts) == 2
    # caller view publishes the empty answer and the parts (an allow-list read).
    view = got.caller_view()
    assert view["answer"] == ""
    assert view["answer_parts"] is not None
    # text search: an empty answer contributes no match, and matching never raises on it.
    assert _record_matches(got, "picture") is False
    assert _record_matches(got, "ask m1") is True  # still matches the inbound text


async def test_claim_delivery_is_exactly_once(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    now = time.time()
    # First worker wins the lease.
    assert await store.claim_delivery("m1", now, "tokA", 120) == 1
    # A second, different worker (a boot re-drive) sees the live lease and is refused.
    assert await store.claim_delivery("m1", now, "tokB", 120) == 0
    # The holder may re-claim (refresh) its own lease.
    assert await store.claim_delivery("m1", now + 1, "tokA", 120) == 1
    # A missing record answers -1.
    assert await store.claim_delivery("gone", now, "tokA", 120) == -1


async def test_claim_delivery_skips_terminal_record(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    await store.mark_delivered("m1", [], 1, time.time(), "tok")
    assert await store.claim_delivery("m1", time.time(), "tok", 120) == 0


async def test_channel_provisional_then_receipt_delivered(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    assert await store.mark_provisional("m1", ["out-1"], 1, time.time(), "tok") == 1
    got = await store.get_record("m1")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.PROVISIONAL
    assert got.outbound_message_ids == ["out-1"]
    # A positive receipt confirms it delivered.
    assert await store.ingest_receipt("m1", DeliveryReceipt.DELIVERED, time.time()) == 1
    got = await store.get_record("m1")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.DELIVERED
    # A repeat receipt is idempotent.
    assert await store.ingest_receipt("m1", DeliveryReceipt.DELIVERED, time.time()) == 0


async def test_receipt_failed_marks_failed(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    await store.mark_provisional("m1", ["out-1"], 1, time.time(), "tok")
    assert await store.ingest_receipt("m1", DeliveryReceipt.FAILED, time.time()) == 1
    got = await store.get_record("m1")
    assert got is not None
    assert got.delivery_status is DeliveryStatus.FAILED


async def test_receipt_conflicts_with_opposite_terminal(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    await store.mark_delivered("m1", [], 1, time.time(), "tok")
    # A late FAILED receipt on an already-delivered record is a conflict, not an override.
    assert await store.ingest_receipt("m1", DeliveryReceipt.FAILED, time.time()) == -2


async def test_mark_delivered_idempotent_and_failed_guard(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    assert await store.mark_delivered("m1", [], 1, time.time(), "tok") == 1
    assert await store.mark_delivered("m1", [], 1, time.time(), "tok") == 0  # already delivered
    assert await store.mark_failed("m1", 1, time.time(), "tok") == -2  # cannot fail a delivered record


async def test_bump_attempt(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record())
    assert await store.bump_attempt("m1") == 1
    assert await store.bump_attempt("m1") == 2


async def test_outbound_reverse_index(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.index_outbound("twilio", ["o-1", "o-2"], "m1")
    assert await store.resolve_outbound("twilio", "o-1") == "m1"
    assert await store.resolve_outbound("twilio", "o-2") == "m1"
    assert await store.resolve_outbound("twilio", "unknown") is None


async def test_pending_work_reports_non_terminal_only(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record("pend"))
    await store.create_record(_record("prov"))
    await store.mark_provisional("prov", ["o"], 1, 1000.0, "tok")
    await store.create_record(_record("done"))
    await store.mark_delivered("done", [], 1, time.time(), "tok")

    work = {w.message_id: w for w in await store.pending_work()}
    assert set(work) == {"pend", "prov"}
    assert work["pend"].delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert work["prov"].delivery_status is DeliveryStatus.PROVISIONAL
    assert work["prov"].grace_deadline is not None


async def test_pending_work_skips_corrupt_rows_and_still_reports_the_rest(monkeypatch):
    # One unreadable row must not abort the pass, or every record behind it is stranded
    # forever.
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    await store.create_record(_record("good"))
    fake.seed_hash(
        settings.record_key("unknown-status"),
        {"data": "{}", "delivery_status": "in_flight", "outbound_ids": "[]", "attempts": "0", "updated_at": "1"},
    )
    fake.seed_hash(
        settings.record_key("non-numeric-attempts"),
        {"data": "{}", "delivery_status": "pending_delivery", "outbound_ids": "[]", "attempts": "?", "updated_at": "1"},
    )
    fake.seed_hash(settings.record_key("foreign-row"), {"something": "else"})
    fake.seed_hash(
        settings.record_key("bad-grace"),
        {
            "data": "{}",
            "delivery_status": "provisional",
            "outbound_ids": "[]",
            "attempts": "1",
            "grace_deadline": "soon",
            "updated_at": "1",
        },
    )
    # The pass reads the status index, so a corrupt row only reaches it while indexed.
    for message_id, indexed_as in (
        ("unknown-status", "pending_delivery"),
        ("non-numeric-attempts", "pending_delivery"),
        ("foreign-row", "pending_delivery"),
        ("bad-grace", "provisional"),
    ):
        await fake.zadd(settings.status_index_key(indexed_as), {message_id: float("inf")})

    work = await store.pending_work()
    assert [w.message_id for w in work] == ["good"]


async def test_list_by_status_failed(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record("a"))
    await store.mark_failed("a", 3, time.time(), "tok")
    await store.create_record(_record("b"))
    await store.mark_delivered("b", [], 1, time.time(), "tok")

    failed = await store.list_by_status(frozenset({DeliveryStatus.FAILED}))
    assert [r.message_id for r in failed] == ["a"]


async def test_retention_ttl_applied_only_on_a_terminal_transition(monkeypatch):
    # A record persisted before send carries NO expiry (it must survive until delivered
    # or failed); the retention TTL is applied exactly on the terminal write.
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    key = settings.record_key("m1")
    await store.create_record(_record("m1"))
    assert key not in fake.ttl_ms  # pending_delivery never expires

    await store.mark_provisional("m1", ["o"], 1, time.time(), "tok")
    assert key not in fake.ttl_ms  # provisional still does not expire

    await store.mark_delivered("m1", ["o"], 1, time.time(), "tok")
    assert fake.ttl_ms[key] == settings.answer_retention_ttl_seconds * 1000


async def test_retention_ttl_applied_on_a_failed_transition(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    key = settings.record_key("m1")
    await store.create_record(_record("m1"))
    await store.mark_failed("m1", 8, time.time(), "tok")
    assert fake.ttl_ms[key] == settings.answer_retention_ttl_seconds * 1000


async def test_message_id_is_a_uuid4(monkeypatch):
    # A record round-trips whatever id it was created with; the doors mint uuid4.
    from uuid import UUID, uuid4

    minted = str(uuid4())
    assert UUID(minted).version == 4
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record(minted))
    got = await store.get_record(minted)
    assert got is not None
    assert got.message_id == minted


# -- the per-status index the listings read -----------------------------------


async def test_a_transition_moves_the_record_between_status_indexes(monkeypatch):
    # Exactly one index names a record, so a listing costs the work outstanding and never
    # walks the retained keyspace.
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    pending = settings.status_index_key(DeliveryStatus.PENDING_DELIVERY.value)
    failed = settings.status_index_key(DeliveryStatus.FAILED.value)
    await store.create_record(_record("m1"))
    assert await fake.zrange(pending, 0, -1) == ["m1"]

    await store.mark_failed("m1", 1, time.time(), "tok")
    assert await fake.zrange(pending, 0, -1) == []
    assert await fake.zrange(failed, 0, -1) == ["m1"]
    # A terminal member is scored to expire with the row it names.
    assert fake._zsets[failed]["m1"] <= time.time() + settings.answer_retention_ttl_seconds
    assert await store.pending_work() == []


async def test_a_terminal_index_member_expires_with_its_row(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_ANSWER_RETENTION_TTL_SECONDS", "1")
    store = _store(monkeypatch, FakeRecordRedis())
    await store.create_record(_record("old"))
    # Terminal a full retention window ago: the index must not name it any more.
    await store.mark_failed("old", 1, time.time() - 10, "tok")

    assert await store.list_by_status(frozenset({DeliveryStatus.FAILED})) == []


async def test_a_member_whose_row_is_gone_is_unindexed(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    await store.create_record(_record("gone"))
    # The row removed from under the index rather than through ``delete_record``.
    fake._hashes.pop(settings.record_key("gone"))

    assert await store.pending_work() == []
    assert await fake.zrange(settings.status_index_key(DeliveryStatus.PENDING_DELIVERY.value), 0, -1) == []


async def test_delete_record_unindexes_the_row_it_removes(monkeypatch):
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    record = _record("m1")
    await store.create_record(record)

    assert await store.delete_record(record) is True
    assert await store.pending_work() == []
    assert (
        await fake.zrange(ConversationsSettings().status_index_key(DeliveryStatus.PENDING_DELIVERY.value), 0, -1) == []
    )


async def test_pending_work_skips_a_row_whose_content_blob_is_corrupt(monkeypatch):
    # Control fields fine, ``data`` unparseable: the whole-row parse must skip it here, not
    # hand it to a delivery that re-reads it unguarded and re-drives it every lease forever.
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    await store.create_record(_record("good"))
    fake.seed_hash(
        settings.record_key("bad-data"),
        {
            "data": "{not json",
            "delivery_status": "pending_delivery",
            "outbound_ids": "[]",
            "attempts": "0",
            "grace_deadline": "",
            "updated_at": "1",
        },
    )
    await fake.zadd(settings.status_index_key("pending_delivery"), {"bad-data": float("inf")})

    work = await store.pending_work()
    assert [w.message_id for w in work] == ["good"]


async def test_prune_expired_terminal_indexes_drops_only_expired_members(monkeypatch):
    # The delivered/shed indexes are read by no listing, so a periodic prune must drop the
    # members whose row has expired or they outgrow the retained keyspace they name.
    fake = FakeRecordRedis()
    store = _store(monkeypatch, fake)
    settings = ConversationsSettings()
    now = time.time()
    for status in (DeliveryStatus.DELIVERED, DeliveryStatus.SHED, DeliveryStatus.FAILED):
        await fake.zadd(
            settings.status_index_key(status.value),
            {f"{status.value}-expired": now - 10, f"{status.value}-live": now + 100_000},
        )

    await store.prune_expired_terminal_indexes([])

    for status in (DeliveryStatus.DELIVERED, DeliveryStatus.SHED, DeliveryStatus.FAILED):
        assert await fake.zrange(settings.status_index_key(status.value), 0, -1) == [f"{status.value}-live"]


async def test_a_record_must_carry_the_inbound_text_it_answers(monkeypatch):
    # Every door records the message it accepted, so ``None`` could only ever mean a row
    # written by an older build — silent tolerance of a shape nothing writes.
    fake = FakeRecordRedis()
    _store(monkeypatch, fake)
    fields = _record().model_dump()
    fields.pop("inbound_text")
    with pytest.raises(ValueError, match="inbound_text"):
        ConversationRecord.model_validate(fields)
    with pytest.raises(ValueError, match="inbound_text"):
        ConversationRecord.model_validate({**fields, "inbound_text": None})
