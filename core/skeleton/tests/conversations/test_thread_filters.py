"""The thread-listing filters and the message text search — the bounded post-scans that
back ``?status=``/``?address=`` on the thread listing, ``?q=`` on the transcript, and the
route-scoped ``messages/search`` leg.

No filter has a direct index (the per-status indexes are global over message ids, not
per-route thread ids), so each is an app-side scan bounded by an explicit budget; a page that
spends its budget reports ``truncated`` LOUDLY rather than silently cutting the result.
"""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_ROUTE = "line"


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


@pytest.fixture
def fake() -> FakeRecordRedis:
    fake = FakeRecordRedis()
    fake.seed_route(_ROUTE)
    return fake


@pytest.fixture
def store(monkeypatch, fake: FakeRecordRedis) -> ConversationRecordStore:
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRecordStore(ConversationsSettings())


def _record(message_id: str, *, created_at: float, thread_id: str, **over) -> ConversationRecord:
    fields = {
        "message_id": message_id,
        "route_name": _ROUTE,
        "door": "channel",
        "thread_id": thread_id,
        "client_address": thread_id.split(":")[-1],
        "channel": "twilio",
        "our_identity": "+15550001111",
        "origin": "client",
        "inbound_text": f"inbound {message_id}",
        "answer_status": "answered",
        "answer": f"answer {message_id}",
        "created_at": created_at,
        "updated_at": created_at,
    }
    fields.update(over)
    return ConversationRecord(**fields)  # type: ignore[arg-type]


# -- status filter -----------------------------------------------------------


async def test_the_thread_listing_filters_by_summary_status(store):
    now = time.time()
    await store.create_record(_record("d0", created_at=now, thread_id="bridge:line:+1000"))
    await store.create_record(
        _record("f0", created_at=now + 1, thread_id="bridge:line:+2000", delivery_status=DeliveryStatus.FAILED)
    )

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10, status=frozenset({DeliveryStatus.FAILED}))

    assert [thread.thread_id for thread in page.threads] == ["bridge:line:+2000"]
    assert page.total == 1
    assert page.truncated is False


# -- address filter ----------------------------------------------------------


async def test_the_thread_listing_filters_by_address_substring(store):
    now = time.time()
    await store.create_record(_record("a0", created_at=now, thread_id="bridge:line:+15550002222"))
    await store.create_record(_record("a1", created_at=now + 1, thread_id="bridge:line:+15559990000"))

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10, address="2222")

    assert [thread.thread_id for thread in page.threads] == ["bridge:line:+15550002222"]
    assert page.total == 1


async def test_the_address_filter_never_matches_a_person_thread(store):
    # A person thread carries no address suffix, so it can never satisfy a non-empty address
    # filter — never a false match on the ``@person`` literal.
    now = time.time()
    await store.create_record(_record("p0", created_at=now, thread_id="bridge:@person:abc"))

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10, address="abc")

    assert page.threads == []
    assert page.total == 0


async def test_status_and_address_filters_compose(store):
    now = time.time()
    await store.create_record(_record("m0", created_at=now, thread_id="bridge:line:+15550002222"))
    await store.create_record(
        _record("m1", created_at=now + 1, thread_id="bridge:line:+15550002200", delivery_status=DeliveryStatus.FAILED)
    )
    await store.create_record(
        _record("m2", created_at=now + 2, thread_id="bridge:line:+15559990000", delivery_status=DeliveryStatus.FAILED)
    )

    page = await store.list_route_threads(
        _ROUTE, offset=0, limit=10, status=frozenset({DeliveryStatus.FAILED}), address="2200"
    )

    assert [thread.thread_id for thread in page.threads] == ["bridge:line:+15550002200"]


async def test_a_filtered_listing_pages_over_matches(store):
    now = time.time()
    for index in range(3):
        await store.create_record(
            _record(
                f"f{index}",
                created_at=now + index,
                thread_id=f"bridge:line:+155500{index}",
                delivery_status=DeliveryStatus.FAILED,
            )
        )
    await store.create_record(_record("d0", created_at=now + 9, thread_id="bridge:line:+9999"))

    first = await store.list_route_threads(_ROUTE, offset=0, limit=2, status=frozenset({DeliveryStatus.FAILED}))
    second = await store.list_route_threads(_ROUTE, offset=2, limit=2, status=frozenset({DeliveryStatus.FAILED}))

    assert first.total == 3
    assert len(first.threads) == 2
    assert [thread.thread_id for thread in second.threads] == ["bridge:line:+1555000"]


async def test_a_filter_scan_that_spends_its_budget_reports_truncated(store, monkeypatch):
    monkeypatch.setattr(records_module, "_FILTER_THREAD_SCAN", 2)
    now = time.time()
    # Three candidate threads, none matching, so the scan spends its whole budget without
    # finishing — reported LOUDLY, never a silent short page.
    for index in range(3):
        await store.create_record(_record(f"m{index}", created_at=now + index, thread_id=f"bridge:line:+100{index}"))

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10, status=frozenset({DeliveryStatus.FAILED}))

    assert page.truncated is True
    assert page.threads == []


async def test_a_filter_scan_reads_the_route_index_in_bounded_windows(store, fake, monkeypatch):
    monkeypatch.setattr(records_module, "_FILTER_SCAN_WINDOW", 2)
    now = time.time()
    for index in range(5):
        await store.create_record(_record(f"m{index}", created_at=now + index, thread_id=f"bridge:line:+100{index}"))
    windows: list[tuple[int, int]] = []
    inner = fake.zrevrange

    async def _windowed(key, start, end, withscores=False):
        if key == ConversationsSettings().route_threads_key(_ROUTE):
            windows.append((start, end))
        return await inner(key, start, end, withscores)

    monkeypatch.setattr(fake, "zrevrange", _windowed)

    await store.list_route_threads(_ROUTE, offset=0, limit=10, address="100")

    assert windows
    assert all(end - start + 1 == 2 for start, end in windows)


# -- transcript q search -----------------------------------------------------

_THREAD = "bridge:line:+15550002222"


async def test_a_transcript_q_search_keeps_only_matching_records(store):
    now = time.time()
    await store.create_record(_record("m0", created_at=now, thread_id=_THREAD, inbound_text="hello world"))
    await store.create_record(_record("m1", created_at=now + 1, thread_id=_THREAD, inbound_text="a widget please"))
    await store.create_record(
        _record("m2", created_at=now + 2, thread_id=_THREAD, inbound_text="ok", answer="your WIDGET is on the way")
    )

    page = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=10, q="widget")

    # Case-insensitive, over BOTH inbound text and answer.
    assert [record.message_id for record in page.records] == ["m1", "m2"]
    assert page.total == 2
    assert page.truncated is False


async def test_a_transcript_q_search_with_no_match_is_an_empty_page_not_total_zero_unknown(store):
    now = time.time()
    await store.create_record(_record("m0", created_at=now, thread_id=_THREAD, inbound_text="hello"))

    page = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=10, q="nothing")

    assert page.records == []
    assert page.total == 0


async def test_a_transcript_q_search_reports_truncated_on_budget(store, monkeypatch):
    monkeypatch.setattr(records_module, "_FILTER_RECORD_SCAN", 2)
    now = time.time()
    for index in range(3):
        await store.create_record(
            _record(f"m{index}", created_at=now + index, thread_id=_THREAD, inbound_text="no match here")
        )

    page = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=10, q="widget")

    assert page.truncated is True


# -- route message search ----------------------------------------------------


async def test_search_route_messages_spans_every_thread(store):
    now = time.time()
    await store.create_record(
        _record("a0", created_at=now, thread_id="bridge:line:+1000", inbound_text="widget for account")
    )
    await store.create_record(_record("a1", created_at=now + 1, thread_id="bridge:line:+1000", inbound_text="thanks"))
    await store.create_record(
        _record("b0", created_at=now + 2, thread_id="bridge:line:+2000", answer="your widget posted")
    )

    found = await store.search_route_messages(_ROUTE, offset=0, limit=10, q="widget")

    assert {record.message_id for record in found.records} == {"a0", "b0"}
    assert found.total == 2
    assert found.truncated is False


async def test_search_route_messages_reports_truncated_on_budget(store, monkeypatch):
    monkeypatch.setattr(records_module, "_FILTER_RECORD_SCAN", 2)
    now = time.time()
    for index in range(3):
        await store.create_record(
            _record(f"m{index}", created_at=now + index, thread_id="bridge:line:+1000", inbound_text="no match")
        )

    found = await store.search_route_messages(_ROUTE, offset=0, limit=10, q="widget")

    assert found.truncated is True


async def test_search_route_messages_skips_a_rowless_member_loudly(store, fake, caplog):
    now = time.time()
    await store.create_record(_record("a0", created_at=now, thread_id="bridge:line:+1000", inbound_text="widget one"))
    await store.create_record(
        _record("a1", created_at=now + 1, thread_id="bridge:line:+1000", inbound_text="widget two")
    )
    fake._hashes.pop(ConversationsSettings().record_key("a0"))

    found = await store.search_route_messages(_ROUTE, offset=0, limit=10, q="widget")

    assert [record.message_id for record in found.records] == ["a1"]
    assert "left for the prune pass" in caplog.text


async def test_search_route_messages_bounds_the_thread_dimension_on_empty_indexes(store, fake, monkeypatch):
    # A route whose members are stranded in the route index but whose per-thread indexes are
    # momentarily EMPTY spends no record budget, so ONLY a thread-dimension bound stops the
    # outer loop — otherwise it is an unbounded scan that never reports truncation.
    monkeypatch.setattr(records_module, "_FILTER_THREAD_SCAN", 3)
    settings = ConversationsSettings()
    route_key = settings.route_threads_key(_ROUTE)
    for index in range(10):
        fake._zsets.setdefault(route_key, {})[f"bridge:line:+{index}"] = float(index)

    found = await store.search_route_messages(_ROUTE, offset=0, limit=10, q="widget")

    assert found.records == []
    assert found.total == 0
    assert found.truncated is True
