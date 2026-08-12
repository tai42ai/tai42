"""The thread indexes — the per-thread transcript index and the per-route thread index —
against the faked redis ZSET + Lua seam.

They are what makes a conversation readable as a conversation: written with the record,
re-scored as the turn resolves, unindexed with the record they name, and reclaimed when the
rows behind them expire.
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
_THREAD = "bridge:line:+15550002222"


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


@pytest.fixture
def fake() -> FakeRecordRedis:
    fake = FakeRecordRedis()
    # The create writes the thread indexes only while the route still routes, so the
    # routing row stands for every test that is not about a route being deleted.
    fake.seed_route(_ROUTE)
    return fake


@pytest.fixture
def store(monkeypatch, fake: FakeRecordRedis) -> ConversationRecordStore:
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRecordStore(ConversationsSettings())


def _record(message_id: str, *, created_at: float, thread_id: str = _THREAD, **over) -> ConversationRecord:
    fields = {
        "message_id": message_id,
        "route_name": _ROUTE,
        "door": "channel",
        "thread_id": thread_id,
        "client_address": "+15550002222",
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


def _thread_key(thread_id: str = _THREAD) -> str:
    return ConversationsSettings().thread_index_key(_ROUTE, thread_id)


def _route_threads_key() -> str:
    return ConversationsSettings().route_threads_key(_ROUTE)


# -- writes ------------------------------------------------------------------


async def test_creating_a_record_indexes_it_in_its_thread_and_the_route(store, fake):
    now = time.time()
    await store.create_record(_record("m1", created_at=now))

    assert await fake.zrange(_thread_key(), 0, -1) == ["m1"]
    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]


async def test_the_transcript_index_is_scored_by_creation_so_it_reads_in_order(store, fake):
    now = time.time()
    # Written newest-first: the index must still read oldest-first.
    await store.create_record(_record("m2", created_at=now + 5))
    await store.create_record(_record("m1", created_at=now))

    assert await fake.zrange(_thread_key(), 0, -1) == ["m1", "m2"]


async def test_the_route_thread_index_tracks_the_last_activity(store, fake):
    now = time.time()
    await store.create_record(
        _record("m1", created_at=now, delivery_status=DeliveryStatus.ACCEPTED, answer_status=None, answer=None),
        intake_token="worker-1",
    )
    assert fake._zsets[_route_threads_key()][_THREAD] == pytest.approx(now)

    completed = _record("m1", created_at=now, updated_at=now + 30)
    assert await store.complete_turn(completed) == 1
    assert fake._zsets[_route_threads_key()][_THREAD] == pytest.approx(now + 30)


async def test_a_silent_turn_re_scores_the_thread_too(store, fake):
    now = time.time()
    await store.create_record(
        _record("m1", created_at=now, delivery_status=DeliveryStatus.ACCEPTED, answer_status=None, answer=None),
        intake_token="worker-1",
    )
    silent = _record("m1", created_at=now, delivery_status=DeliveryStatus.SILENT, answer_status=None, answer=None)
    assert await store.complete_silent(silent) == 1
    assert fake._zsets[_route_threads_key()][_THREAD] >= now


async def test_deleting_the_last_record_takes_its_thread_out_of_the_route_index(store, fake):
    now = time.time()
    first = _record("m1", created_at=now)
    second = _record("m2", created_at=now + 1)
    await store.create_record(first)
    await store.create_record(second)

    # A thread outlives any ONE of its records.
    assert await store.delete_record(first) is True
    assert await fake.zrange(_thread_key(), 0, -1) == ["m2"]
    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]

    assert await store.delete_record(second) is True
    assert await fake.zrange(_thread_key(), 0, -1) == []
    assert await fake.zrange(_route_threads_key(), 0, -1) == []


# -- reads -------------------------------------------------------------------


async def test_the_thread_listing_summarizes_each_thread_from_its_newest_record(store):
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    await store.create_record(_record("m2", created_at=now + 10, delivery_status=DeliveryStatus.FAILED))
    await store.create_record(_record("o1", created_at=now + 20, thread_id="bridge:line:+15550009999"))

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10)

    assert page.total == 2
    # Newest activity first.
    assert [thread.thread_id for thread in page.threads] == ["bridge:line:+15550009999", _THREAD]
    summary = page.threads[1]
    assert summary.message_count == 2
    assert summary.last_delivery_status is DeliveryStatus.FAILED
    assert summary.last_activity_at == pytest.approx(now + 10)
    assert summary.client_address == "+15550002222"


async def test_the_listed_activity_moment_is_the_key_the_listing_sorts_by(store, fake):
    # A later delivery transition moves the record's ``updated_at`` but NOT the thread's
    # place in the route index. The summary must carry the moment it was ORDERED by, or the
    # door advertises "newest activity first" and hands back rows out of that order.
    now = time.time()
    await store.create_record(
        _record("m1", created_at=now, delivery_status=DeliveryStatus.ACCEPTED, answer_status=None, answer=None),
        intake_token="worker-1",
    )
    assert await store.complete_turn(_record("m1", created_at=now, updated_at=now + 30)) == 1
    assert await store.mark_delivered("m1", [], attempts=1, now=now + 900, token="worker-1") == 1

    (summary,) = (await store.list_route_threads(_ROUTE, offset=0, limit=10)).threads

    # An absolute tolerance: these moments are ~1.8e9 apart from zero, so the default
    # RELATIVE one would swallow the whole 870-second discrepancy this test exists to catch.
    assert summary.last_activity_at == pytest.approx(now + 30, abs=1e-6)
    assert summary.last_activity_at == pytest.approx(fake._zsets[_route_threads_key()][_THREAD], abs=1e-6)


async def test_the_thread_listing_pages(store):
    now = time.time()
    for index in range(3):
        await store.create_record(
            _record(f"m{index}", created_at=now + index, thread_id=f"bridge:line:+1555000{index}")
        )

    first = await store.list_route_threads(_ROUTE, offset=0, limit=2)
    second = await store.list_route_threads(_ROUTE, offset=2, limit=2)

    assert [thread.thread_id for thread in first.threads] == ["bridge:line:+15550002", "bridge:line:+15550001"]
    assert [thread.thread_id for thread in second.threads] == ["bridge:line:+15550000"]
    assert first.total == second.total == 3


async def test_the_transcript_reads_oldest_first_and_pages(store):
    now = time.time()
    for index in range(3):
        await store.create_record(_record(f"m{index}", created_at=now + index))

    first = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=2)
    second = await store.list_thread_records(_ROUTE, _THREAD, offset=2, limit=2)

    assert [record.message_id for record in first.records] == ["m0", "m1"]
    assert [record.message_id for record in second.records] == ["m2"]
    assert first.total == 3
    assert first.records[0].inbound_text == "inbound m0"


async def test_the_transcript_reads_newest_first_and_pages_from_that_end(store):
    # The live-tail order: page 1 holds the LATEST messages, so a poller re-reading page 1
    # sees each new message instead of the first page of history forever.
    now = time.time()
    for index in range(3):
        await store.create_record(_record(f"m{index}", created_at=now + index))

    first = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=2, newest_first=True)
    second = await store.list_thread_records(_ROUTE, _THREAD, offset=2, limit=2, newest_first=True)

    assert [record.message_id for record in first.records] == ["m2", "m1"]
    assert [record.message_id for record in second.records] == ["m0"]
    assert first.total == second.total == 3


async def test_an_unknown_thread_reads_as_an_empty_index(store):
    page = await store.list_thread_records(_ROUTE, "bridge:line:nobody", offset=0, limit=10)
    assert page.total == 0
    assert page.records == []


# -- reads never mutate the indexes ------------------------------------------


async def test_a_transcript_read_leaves_a_rowless_member_for_the_prune_pass(store, fake, caplog):
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    await store.create_record(_record("m2", created_at=now + 1))
    # The row swept by the retention TTL from under its index.
    fake._hashes.pop(ConversationsSettings().record_key("m1"))

    page = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=10)

    assert [record.message_id for record in page.records] == ["m2"]
    # The index is untouched: the read reports what it walked over and reclaims nothing.
    assert await fake.zrange(_thread_key(), 0, -1) == ["m1", "m2"]
    assert page.total == 2
    assert "left for the prune pass" in caplog.text


async def test_paging_a_transcript_holding_rowless_members_never_skips_a_live_record(store, fake):
    # A read that unindexed as it walked shortened the index UNDER its own offsets, so the
    # next page started past records it had never returned.
    now = time.time()
    for index in range(6):
        await store.create_record(_record(f"m{index}", created_at=now + index))
    for gone in ("m0", "m2"):
        fake._hashes.pop(ConversationsSettings().record_key(gone))

    seen: list[str] = []
    for page in range(3):
        window = await store.list_thread_records(_ROUTE, _THREAD, offset=page * 2, limit=2)
        seen.extend(record.message_id for record in window.records)
        assert window.total == 6

    assert seen == ["m1", "m3", "m4", "m5"]


async def test_a_thread_listing_leaves_a_thread_with_no_readable_record_for_the_prune_pass(store, fake, caplog):
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    fake._hashes.pop(ConversationsSettings().record_key("m1"))

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10)

    # Not shown — it has nothing to summarize — but not unindexed by the read either.
    assert page.threads == []
    assert page.total == 1
    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]
    assert "left for the prune pass" in caplog.text


async def test_paging_a_thread_listing_holding_dead_threads_never_skips_a_live_one(store, fake):
    now = time.time()
    for index in range(5):
        await store.create_record(
            _record(f"m{index}", created_at=now + index, thread_id=f"bridge:line:+1555000{index}")
        )
    for gone in ("m4", "m2"):
        fake._hashes.pop(ConversationsSettings().record_key(gone))

    seen: list[str] = []
    for page in range(3):
        window = await store.list_route_threads(_ROUTE, offset=page * 2, limit=2)
        seen.extend(thread.thread_id for thread in window.threads)
        assert window.total == 5

    assert seen == ["bridge:line:+15550003", "bridge:line:+15550001", "bridge:line:+15550000"]


# -- retention ---------------------------------------------------------------


async def test_the_prune_pass_reclaims_an_idle_thread_and_leaves_a_live_one(store, fake):
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    await store.create_record(_record("old", created_at=now - retention - 60, thread_id="bridge:line:+15550000000"))
    await store.create_record(_record("new", created_at=now))
    # Only the idle thread's row has been swept by the retention TTL.
    fake._hashes.pop(ConversationsSettings().record_key("old"))

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]
    assert await fake.zrange(_thread_key("bridge:line:+15550000000"), 0, -1) == []
    assert await fake.zrange(_thread_key(), 0, -1) == ["new"]


async def test_the_prune_pass_keeps_an_idle_thread_whose_rows_are_still_retained(store, fake):
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    # Old enough to be a candidate, but its row is still there — the row, not the clock,
    # decides.
    await store.create_record(_record("old", created_at=now - retention - 60))

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]
    assert await fake.zrange(_thread_key(), 0, -1) == ["old"]


async def test_the_prune_pass_reclaims_the_expired_members_of_an_ACTIVE_thread(store, fake):
    # A long-lived thread never goes idle, so its aged-out members are reclaimable only
    # here; unreclaimed, its ``message_count`` grows forever past what is readable.
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    for index in range(3):
        await store.create_record(_record(f"old{index}", created_at=now - retention - 60 + index))
    await store.create_record(_record("live", created_at=now))
    for index in range(3):
        fake._hashes.pop(ConversationsSettings().record_key(f"old{index}"))
    assert (await store.list_route_threads(_ROUTE, offset=0, limit=10)).threads[0].message_count == 4

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert await fake.zrange(_thread_key(), 0, -1) == ["live"]
    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]
    assert (await store.list_route_threads(_ROUTE, offset=0, limit=10)).threads[0].message_count == 1


async def test_the_prune_pass_reads_a_thread_in_bounded_batches(store, fake, monkeypatch):
    monkeypatch.setattr(records_module, "_PRUNE_MEMBERS_PER_BATCH", 2)
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    for index in range(5):
        await store.create_record(_record(f"old{index}", created_at=now - retention - 60 + index))
        fake._hashes.pop(ConversationsSettings().record_key(f"old{index}"))
    reads: list[int] = []
    inner = fake.zrangebyscore

    async def _counted(key, minimum, maximum, start=None, num=None):
        found = await inner(key, minimum, maximum, start=start, num=num)
        reads.append(len(found))
        return found

    monkeypatch.setattr(fake, "zrangebyscore", _counted)

    await store.prune_expired_terminal_indexes([_ROUTE])

    # Drained within the pass, but never in one unbounded read: no batch is larger than the
    # cap, whatever the thread holds.
    assert await fake.zrange(_thread_key(), 0, -1) == []
    assert await fake.zrange(_route_threads_key(), 0, -1) == []
    assert reads
    assert max(reads) <= 2


async def test_the_prune_pass_charges_its_budget_for_every_thread_it_examines(store, fake, monkeypatch):
    # A HEALTHY thread costs a round trip and reclaims nothing. Charged nothing for it, a
    # pass walks every thread of a route with a hundred thousand of them, every 60 seconds.
    monkeypatch.setattr(records_module, "_PRUNE_WORK_PER_PASS", 2)
    now = time.time()
    for index in range(5):
        await store.create_record(_record(f"m{index}", created_at=now, thread_id=f"bridge:line:+1555000{index}"))
    examined: list[str] = []
    inner = fake.zrangebyscore

    async def _counted(key, minimum, maximum, start=None, num=None):
        examined.append(key)
        return await inner(key, minimum, maximum, start=start, num=num)

    monkeypatch.setattr(fake, "zrangebyscore", _counted)

    cursor = await store.prune_expired_terminal_indexes([_ROUTE])

    assert len(examined) == 2
    # And the pass says where it stopped — the rank AND the thread standing at it, so the
    # next one starts at thread three and can tell whether it is still the same thread.
    assert cursor == records_module.PruneCursor(_ROUTE, 2, 0, "bridge:line:+15550002")


async def test_the_prune_pass_resumes_where_the_last_one_stopped(store, fake, monkeypatch):
    # Without a cursor every pass re-reads the same head of the same index: a run of aged
    # members whose rows are still live absorbs the whole budget forever and the reclaimable
    # thread behind them is never reached.
    monkeypatch.setattr(records_module, "_PRUNE_WORK_PER_PASS", 2)
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    for index in range(3):
        # Aged past the retention window, but the rows are still there — a record carries no
        # expiry until it turns terminal, so nothing here is reclaimable.
        await store.create_record(
            _record(f"live{index}", created_at=now - retention - 60 + index, thread_id=f"bridge:line:+1555000{index}")
        )
    await store.create_record(_record("dead", created_at=now - retention - 30, thread_id="bridge:line:+15550009"))
    fake._hashes.pop(ConversationsSettings().record_key("dead"))

    cursor = records_module.PRUNE_START
    for _ in range(4):
        cursor = await store.prune_expired_terminal_indexes([_ROUTE], cursor)

    # The reclaimable thread is reached, and the live ones are all still indexed.
    assert await fake.zrange(_thread_key("bridge:line:+15550009"), 0, -1) == []
    assert await fake.zrange(_route_threads_key(), 0, -1) == [
        "bridge:line:+15550000",
        "bridge:line:+15550001",
        "bridge:line:+15550002",
    ]


async def test_the_prune_pass_walks_the_route_index_in_bounded_windows(store, fake, monkeypatch):
    # ``ZRANGE key 0 -1`` on a route minting a thread per web visitor is an O(N) reply on
    # every 60-second pass, in front of delivery recovery.
    monkeypatch.setattr(records_module, "_PRUNE_THREADS_PER_BATCH", 2)
    now = time.time()
    for index in range(5):
        await store.create_record(_record(f"m{index}", created_at=now, thread_id=f"bridge:line:+1555000{index}"))
    windows: list[tuple[int, int]] = []
    inner = fake.zrange

    async def _windowed(key, start, end, withscores=False):
        if key == _route_threads_key():
            windows.append((start, end))
        return await inner(key, start, end, withscores)

    monkeypatch.setattr(fake, "zrange", _windowed)

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert windows
    assert all(end - start + 1 == 2 for start, end in windows)


async def test_the_prune_pass_wraps_to_the_start_once_every_route_is_walked(store, fake):
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    assert await store.prune_expired_terminal_indexes([_ROUTE]) == records_module.PRUNE_START


async def test_the_prune_pass_ignores_a_cursor_naming_a_deleted_route(store, fake):
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    await store.create_record(_record("old", created_at=now - retention - 60))
    fake._hashes.pop(ConversationsSettings().record_key("old"))

    await store.prune_expired_terminal_indexes([_ROUTE], records_module.PruneCursor("deleted-route", 7, 9))

    assert await fake.zrange(_route_threads_key(), 0, -1) == []


async def test_the_prune_pass_drops_index_members_only_inside_the_atomic_step(store, fake, monkeypatch):
    # The read-repair shape — count the thread index from Python, then ZREM the thread from
    # the route index — loses to a concurrent create landing between the two, which it then
    # unindexes. Every drop this pass makes must therefore be issued by the script, so
    # nothing here may reach a bare ZREM.
    async def _refuse(*args, **kwargs):
        raise AssertionError("the prune pass must drop index members inside its atomic script, not by a bare ZREM")

    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    await store.create_record(_record("old", created_at=now - retention - 60))
    fake._hashes.pop(ConversationsSettings().record_key("old"))
    monkeypatch.setattr(fake, "zrem", _refuse)

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert await fake.zrange(_thread_key(), 0, -1) == []
    assert await fake.zrange(_route_threads_key(), 0, -1) == []


# -- route deletion ----------------------------------------------------------


async def test_dropping_a_route_reclaims_its_thread_indexes(store, fake):
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    await store.create_record(_record("o1", created_at=now + 1, thread_id="bridge:line:+15550009999"))

    await store.drop_route_threads(_ROUTE)

    # Neither index carries a TTL and the prune only walks live routes, so a delete that
    # left these behind stranded them forever.
    assert _route_threads_key() not in fake._zsets
    assert _thread_key() not in fake._zsets
    assert _thread_key("bridge:line:+15550009999") not in fake._zsets


async def test_dropping_a_route_reads_its_thread_index_in_bounded_windows(store, fake, monkeypatch):
    # ``ZRANGE key 0 -1`` on a route holding a thread per web visitor is one O(N) reply.
    monkeypatch.setattr(records_module, "_PRUNE_THREADS_PER_BATCH", 2)
    now = time.time()
    for index in range(5):
        await store.create_record(_record(f"m{index}", created_at=now, thread_id=f"bridge:line:+1555000{index}"))
    windows: list[tuple[int, int]] = []
    inner = fake.zrange

    async def _windowed(key, start, end, withscores=False):
        windows.append((start, end))
        return await inner(key, start, end, withscores)

    monkeypatch.setattr(fake, "zrange", _windowed)

    await store.drop_route_threads(_ROUTE)

    assert windows
    assert all(end - start + 1 == 2 for start, end in windows)
    assert _route_threads_key() not in fake._zsets
    for index in range(5):
        assert _thread_key(f"bridge:line:+1555000{index}") not in fake._zsets


async def test_a_create_landing_after_the_route_delete_indexes_nothing(store, fake, caplog):
    # A door resolves its route a round trip before the record is created, so a delete
    # completing in that window has already reclaimed both indexes. Re-creating them leaves
    # a pair nothing walks: no read reaches it, no TTL expires it, and the prune walks LIVE
    # routes only.
    now = time.time()
    fake.drop_route(_ROUTE)

    await store.create_record(_record("m1", created_at=now))

    assert await fake.zrange(_thread_key(), 0, -1) == []
    assert await fake.zrange(_route_threads_key(), 0, -1) == []
    # The record still stands — the delivery machine reads the status index, not the
    # thread one — and the silence is reported, never swallowed.
    assert (await store.get_record("m1")) is not None
    assert "stopped routing" in caplog.text


async def test_the_prune_pass_reclaims_a_thread_whose_index_is_already_empty(store, fake):
    # A thread that can offer no candidate at all is examined and skipped by every pass,
    # so nothing ever drops it: the route's ``total`` over-counts it forever, ``next_page``
    # points at an all-omitted page, and a door edit is blocked behind a count with no
    # visible thread.
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    # The transcript index gone from under the route index — what an interrupted route
    # reclamation leaves behind (it deletes the thread's own index first).
    fake._zsets.pop(_thread_key())

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert await fake.zrange(_route_threads_key(), 0, -1) == []


async def test_the_prune_pass_leaves_a_thread_that_still_holds_a_live_member(store, fake):
    # The flip side: a thread with members but no EXPIRED member is healthy, and the same
    # step must leave it exactly where it is.
    now = time.time()
    await store.create_record(_record("m1", created_at=now))

    await store.prune_expired_terminal_indexes([_ROUTE])

    assert await fake.zrange(_route_threads_key(), 0, -1) == [_THREAD]
    assert await fake.zrange(_thread_key(), 0, -1) == ["m1"]


async def _two_reclaimable_members(store, fake, thread_id: str) -> None:
    """Two members of ``thread_id``, both aged past the retention window and both with
    their row already swept — so a pass that reads the thread from rank 0 reclaims both."""
    now = time.time()
    retention = ConversationsSettings().answer_retention_ttl_seconds
    for index in range(2):
        await store.create_record(_record(f"dead{index}", created_at=now - retention - 60 + index, thread_id=thread_id))
        fake._hashes.pop(ConversationsSettings().record_key(f"dead{index}"))


async def test_a_resumed_member_rank_applies_to_the_thread_it_was_measured_in(store, fake):
    # The cursor's whole point: a run of members whose rows are still live would absorb
    # every pass's budget, so the next pass resumes PAST them rather than re-reading them.
    thread = "bridge:line:+15550001"
    await _two_reclaimable_members(store, fake, thread)

    await store.prune_expired_terminal_indexes([_ROUTE], records_module.PruneCursor(_ROUTE, 0, 1, thread))

    # Resumed at rank 1, so only the second member was offered this pass.
    assert await fake.zrange(_thread_key(thread), 0, -1) == ["dead0"]


async def test_a_resumed_member_rank_is_never_applied_to_a_different_thread(store, fake):
    # A thread that leaves the route index between two passes shifts every thread behind it
    # down one rank, so a rank recorded by the last pass can name a DIFFERENT thread on the
    # next one. Carrying the member offset into it silently skips that thread's first
    # members — a pass that spends its budget and reclaims nothing.
    thread = "bridge:line:+15550001"
    await _two_reclaimable_members(store, fake, thread)

    # The cursor stopped one member into a thread that has since left the route index,
    # which is what shifted this one down into the rank it recorded.
    await store.prune_expired_terminal_indexes(
        [_ROUTE], records_module.PruneCursor(_ROUTE, 0, 1, "bridge:line:+15550000")
    )

    # Neither member is skipped: the offset belonged to a thread that is no longer here.
    assert await fake.zrange(_thread_key(thread), 0, -1) == []
    assert await fake.zrange(_route_threads_key(), 0, -1) == []


# -- a route delete racing an in-flight turn ----------------------------------


@pytest.mark.parametrize("outcome", ["completed", "silent"])
async def test_a_turn_completing_after_a_route_delete_never_resurrects_the_index(store, fake, outcome):
    # The route's indexes are reclaimed by its delete and nothing walks the name afterwards,
    # so a completion that re-stamped the route index would leave a thread nothing can
    # reclaim: no read reaches it, no TTL expires it, and the prune walks LIVE routes only.
    now = time.time()
    await store.create_record(
        _record("m1", created_at=now, delivery_status=DeliveryStatus.ACCEPTED, answer_status=None, answer=None),
        intake_token="worker-1",
    )
    await store.drop_route_threads(_ROUTE)

    if outcome == "completed":
        assert await store.complete_turn(_record("m1", created_at=now, updated_at=now + 5)) == 1
    else:
        silent = _record(
            "m1",
            created_at=now,
            delivery_status=DeliveryStatus.SILENT,
            answer_status=None,
            answer=None,
        )
        assert await store.complete_silent(silent) == 1

    assert await fake.zrange(_route_threads_key(), 0, -1) == []
    assert await fake.zrange(_thread_key(), 0, -1) == []


# -- one corrupt row never hides a live thread or a live record ---------------


async def test_the_thread_listing_scans_past_a_corrupt_record_to_the_next_readable_one(store, fake, caplog):
    # A thread with 19 readable records and one corrupt NEWEST one is a live thread; giving
    # up on it removes it from the operator listing permanently, because the prune never
    # reclaims a thread whose rows are all there.
    now = time.time()
    await store.create_record(_record("m1", created_at=now))
    await store.create_record(_record("m2", created_at=now + 1))
    fake.seed_hash(
        ConversationsSettings().record_key("m2"),
        {"data": "{not json", "delivery_status": "delivered", "outbound_ids": "[]", "attempts": "0", "updated_at": "0"},
    )

    page = await store.list_route_threads(_ROUTE, offset=0, limit=10)

    assert [thread.thread_id for thread in page.threads] == [_THREAD]
    assert page.threads[0].message_count == 2
    assert "corrupt" in caplog.text


async def test_a_transcript_skips_a_corrupt_record_and_keeps_the_readable_ones(store, fake, caplog):
    # A regression here turns a transcript read into an unmapped 500 for the whole thread.
    now = time.time()
    for index in range(3):
        await store.create_record(_record(f"m{index}", created_at=now + index))
    fake.seed_hash(
        ConversationsSettings().record_key("m1"),
        {"data": "{not json", "delivery_status": "delivered", "outbound_ids": "[]", "attempts": "0", "updated_at": "0"},
    )

    page = await store.list_thread_records(_ROUTE, _THREAD, offset=0, limit=10)

    assert [record.message_id for record in page.records] == ["m0", "m2"]
    # The index is left alone, so the total the caller pages against is unchanged.
    assert page.total == 3
    assert await fake.zrange(_thread_key(), 0, -1) == ["m0", "m1", "m2"]
    assert "corrupt" in caplog.text


# -- the persist seam ---------------------------------------------------------


async def test_a_non_finite_number_is_refused_at_the_persist_seam(store):
    # ``Infinity``/``NaN`` render as bare tokens no standard JSON parser reads back, so the
    # row would persist unreadable and every later read of it would blow up.
    with pytest.raises(ValueError, match="not JSON compliant"):
        await store.create_record(_record("m1", created_at=float("inf")))
