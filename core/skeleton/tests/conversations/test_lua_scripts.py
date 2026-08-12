"""End-to-end execution of the conversation record store's Redis Lua scripts.

Every exactly-once decision the bridge makes is a server-side Lua step. The rest of the
suite drives a Python re-implementation; this module runs the REAL script text against
``fakeredis[lua]``.

The scripts are driven through :class:`ConversationRecordStore`, so the argument
marshalling each one depends on is exercised with them.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fakeredis import aioredis
from tai42_contract.conversations import DeliveryReceipt

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

_LEASE = 120.0


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


@pytest.fixture
async def lua_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[aioredis.FakeRedis]:
    """A Lua-executing fake Redis wired behind the record store's ``client_ctx`` seam,
    decoding responses exactly as the kit's real client does."""
    client = aioredis.FakeRedis(decode_responses=True)
    # The create script writes the thread indexes only while the route still routes, so
    # the routing row every record here names stands from the start.
    await client.set(ConversationsSettings().route_key("line"), "{}")

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield client

    monkeypatch.setattr(records_module, "client_ctx", fake_client_ctx)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def store(lua_redis: aioredis.FakeRedis) -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _record(message_id: str, *, status: DeliveryStatus = DeliveryStatus.PENDING_DELIVERY) -> ConversationRecord:
    answered = status not in (DeliveryStatus.ACCEPTED, DeliveryStatus.SHED)
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        provider_message_id=f"PID-{message_id}",
        origin="client",
        inbound_text=f"ask {message_id}",
        delivery_status=status,
        answer_status="answered" if answered else None,
        answer="the answer" if answered else None,
        created_at=now,
        updated_at=now,
    )


def _key(message_id: str) -> str:
    return ConversationsSettings().record_key(message_id)


# -- the inbound claim (conversations:dedupe:claim) ---------------------------


async def test_the_inbound_claim_is_get_or_set(store, lua_redis):
    assert await store.claim_inbound("twilio", "PID1", "first") == "first"
    # A second attempt on the same pair is answered with the id that owns it, and does not
    # overwrite it — the arbitration two concurrent accepts resolve through.
    assert await store.claim_inbound("twilio", "PID1", "second") == "first"
    assert await lua_redis.get(ConversationsSettings().dedupe_key("twilio", "PID1")) == "first"


async def test_the_inbound_claim_carries_the_dedupe_ttl(store, lua_redis):
    await store.claim_inbound("twilio", "PID1", "first")
    ttl = await lua_redis.ttl(ConversationsSettings().dedupe_key("twilio", "PID1"))
    assert 0 < ttl <= ConversationsSettings().inbound_dedupe_ttl_seconds


# -- record creation (conversations:record:create) ----------------------------


async def test_a_non_terminal_record_is_created_without_an_expiry(store, lua_redis):
    await store.create_record(_record("m1", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    assert await lua_redis.ttl(_key("m1")) == -1
    assert (await store.get_record("m1")).delivery_status is DeliveryStatus.ACCEPTED


async def test_a_record_created_terminal_carries_the_retention_ttl(store, lua_redis):
    await store.create_record(_record("m-shed", status=DeliveryStatus.SHED))
    ttl = await lua_redis.ttl(_key("m-shed"))
    assert 0 < ttl <= ConversationsSettings().answer_retention_ttl_seconds


# -- the guarded turn completion (conversations:record:complete_turn) ---------


async def test_complete_turn_transitions_only_from_intake(store):
    await store.create_record(_record("m1", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    completed = _record("m1")

    assert await store.complete_turn(completed) == 1
    record = await store.get_record("m1")
    assert record.delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert record.answer == "the answer"
    # The second writer — a turn finishing after a re-drive already resolved the record —
    # is refused rather than overwriting the outcome the client was given.
    assert await store.complete_turn(completed) == 0


async def test_complete_turn_reports_a_missing_record(store):
    assert await store.complete_turn(_record("gone")) == -1


# -- the leased intake claim (conversations:record:intake_claim) ---------------


async def test_an_intake_record_is_created_holding_its_lease_and_no_other_state_is(store, lua_redis):
    await store.create_record(_record("m-intake", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    claim = await lua_redis.hget(_key("m-intake"), "intake_claim")
    assert claim.startswith("worker-1:")
    assert float(claim.split(":", 1)[1]) > time.time()

    await store.create_record(_record("m-pending"))
    assert await lua_redis.hget(_key("m-pending"), "intake_claim") == ""


async def test_creating_a_record_without_the_matching_intake_lease_is_refused(store):
    with pytest.raises(ValueError, match="intake lease"):
        await store.create_record(_record("m-intake", status=DeliveryStatus.ACCEPTED))
    with pytest.raises(ValueError, match="intake lease"):
        await store.create_record(_record("m-pending"), intake_token="worker-1")


async def test_the_intake_claim_refuses_a_live_lease_and_admits_a_lapsed_one(store):
    await store.create_record(_record("m1", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    now = time.time()

    # A booting sibling cannot adopt a record whose turn is still running...
    assert await store.claim_intake("m1", now, "worker-2", _LEASE) == 0
    # ...while the worker running that turn refreshes its own lease. The refresh must carry
    # the expiry FORWARD, or a turn longer than one lease is reapable the moment it renews.
    assert await store.claim_intake("m1", now + _LEASE - 1, "worker-1", _LEASE) == 1
    assert await store.claim_intake("m1", now + _LEASE + 1, "worker-2", _LEASE) == 0
    # Only once the REFRESHED lease has genuinely lapsed is the record adoptable.
    assert await store.claim_intake("m1", now + 2 * _LEASE, "worker-2", _LEASE) == 1


async def test_the_intake_claim_refuses_a_record_that_has_left_intake(store):
    await store.create_record(_record("m1", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    assert await store.complete_turn(_record("m1")) == 1
    assert await store.claim_intake("m1", time.time(), "worker-1", _LEASE) == -2


async def test_the_intake_claim_reports_a_missing_record(store):
    assert await store.claim_intake("gone", time.time(), "worker-1", _LEASE) == -1


async def test_completing_a_turn_releases_the_intake_lease(store, lua_redis):
    await store.create_record(_record("m1", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    assert await store.complete_turn(_record("m1")) == 1
    assert await lua_redis.hget(_key("m1"), "intake_claim") == ""


# -- the leased delivery claim (conversations:record:claim) -------------------


async def test_the_delivery_claim_refuses_a_live_lease_and_admits_a_lapsed_one(store):
    await store.create_record(_record("m1"))
    now = time.time()

    assert await store.claim_delivery("m1", now, "worker-1", _LEASE) == 1
    # Another worker's live lease is not stealable...
    assert await store.claim_delivery("m1", now + 1, "worker-2", _LEASE) == 0
    # ...while the holder refreshes its own lease across a long send.
    assert await store.claim_delivery("m1", now + 1, "worker-1", _LEASE) == 1
    # Once the lease has genuinely lapsed the record is reclaimable by anyone.
    assert await store.claim_delivery("m1", now + 1 + _LEASE + 1, "worker-2", _LEASE) == 1


async def test_the_delivery_claim_refuses_an_intake_record(store):
    await store.create_record(_record("m-intake", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    assert await store.claim_delivery("m-intake", time.time(), "worker-1", _LEASE) == -2


async def test_the_delivery_claim_refuses_a_terminal_record(store):
    await store.create_record(_record("m-shed", status=DeliveryStatus.SHED))
    assert await store.claim_delivery("m-shed", time.time(), "worker-1", _LEASE) == 0


async def test_the_delivery_claim_refuses_a_provisional_record(store):
    """A fully sent record awaits a receipt, not a re-send: only pending_delivery is
    claimable for a send, so a racing re-drive claims nothing."""
    await store.create_record(_record("m-prov"))
    now = time.time()
    assert await store.claim_delivery("m-prov", now, "worker-1", _LEASE) == 1
    assert await store.mark_provisional("m-prov", ["out-1"], 1, now, "worker-1") == 1
    assert await store.claim_delivery("m-prov", now, "worker-2", _LEASE) == 0


async def test_the_delivery_claim_reports_a_missing_record(store):
    assert await store.claim_delivery("gone", time.time(), "worker-1", _LEASE) == -1


# -- provisional + the terminal writes ----------------------------------------


async def test_mark_provisional_records_the_ids_and_releases_the_lease(store, lua_redis):
    await store.create_record(_record("m1"))
    now = time.time()
    assert await store.claim_delivery("m1", now, "worker-1", _LEASE) == 1

    assert await store.mark_provisional("m1", ["out-1", "out-2"], 1, now, "worker-1") == 1
    record = await store.get_record("m1")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.outbound_message_ids == ["out-1", "out-2"]
    assert await lua_redis.hget(_key("m1"), "claim") == ""
    grace = float(await lua_redis.hget(_key("m1"), "grace_deadline"))
    assert grace == pytest.approx(now + ConversationsSettings().delivery_grace_seconds)
    # A provisional record is still non-terminal, so it carries no expiry yet.
    assert await lua_redis.ttl(_key("m1")) == -1


async def test_mark_provisional_refuses_a_terminal_record(store, lua_redis):
    """A receipt or a re-drive can terminalise a record between its last chunk and the
    provisional write; resurrecting it would put a settled answer back in the sweep."""
    await store.create_record(_record("m1"))
    now = time.time()
    assert await store.mark_failed("m1", 1, now, "tok") == 1

    assert await store.mark_provisional("m1", ["out-1"], 1, now, "tok") == 0
    record = await store.get_record("m1")
    assert record.delivery_status is DeliveryStatus.FAILED
    assert record.outbound_message_ids == []
    assert await lua_redis.hget(_key("m1"), "grace_deadline") == ""


async def test_mark_provisional_reports_a_missing_record(store, lua_redis):
    """A record swept out from under the sender must stay gone: writing the fields back
    resurrects a hash with no content blob, which every later read blows up on."""
    assert await store.mark_provisional("gone", ["out-1"], 1, time.time(), "tok") == -1
    assert await lua_redis.exists(_key("gone")) == 0


async def test_the_terminal_writes_are_idempotent_and_refuse_the_opposite_outcome(store, lua_redis):
    await store.create_record(_record("m-ok"))
    now = time.time()

    assert await store.mark_delivered("m-ok", ["out-1"], 1, now, "tok") == 1
    assert await store.mark_delivered("m-ok", ["out-1"], 1, now, "tok") == 0
    assert await store.mark_failed("m-ok", 1, now, "tok") == -2
    assert (await store.get_record("m-ok")).outbound_message_ids == ["out-1"]
    assert 0 < await lua_redis.ttl(_key("m-ok")) <= ConversationsSettings().answer_retention_ttl_seconds

    await store.create_record(_record("m-bad"))
    assert await store.mark_failed("m-bad", 8, now, "tok") == 1
    assert await store.mark_failed("m-bad", 8, now, "tok") == 0
    assert await store.mark_delivered("m-bad", [], 8, now, "tok") == -2
    # BOTH terminal writes carry the retention TTL, or a failed record leaks forever.
    assert 0 < await lua_redis.ttl(_key("m-bad")) <= ConversationsSettings().answer_retention_ttl_seconds

    assert await store.mark_delivered("gone", [], 1, now, "tok") == -1
    assert await store.mark_failed("gone", 1, now, "tok") == -1
    assert await lua_redis.exists(_key("gone")) == 0


@pytest.mark.parametrize("settle", ["provisional", "delivered", "failed", "receipt"])
async def test_a_settled_record_holds_no_lease_and_no_stale_grace_deadline(store, lua_redis, settle):
    """Every write that ends this worker's send releases the lease, and every terminal one
    also drops the grace deadline — leftovers a later pass would act on."""
    await store.create_record(_record("m1"))
    now = time.time()
    assert await store.claim_delivery("m1", now, "worker-1", _LEASE) == 1

    if settle == "delivered":
        assert await store.mark_delivered("m1", ["out-1"], 1, now, "worker-1") == 1
    elif settle == "failed":
        assert await store.mark_failed("m1", 1, now, "worker-1") == 1
    else:
        assert await store.mark_provisional("m1", ["out-1"], 1, now, "worker-1") == 1

    if settle == "receipt":
        assert await store.ingest_receipt("m1", DeliveryReceipt.DELIVERED, now) == 1

    assert await lua_redis.hget(_key("m1"), "claim") == ""
    if settle != "provisional":
        assert await lua_redis.hget(_key("m1"), "grace_deadline") == ""


@pytest.mark.parametrize("write", ["provisional", "delivered", "failed"])
async def test_a_delivery_write_is_refused_under_another_workers_live_lease(store, write):
    """A worker whose lease lapsed and was taken over must not be able to write ANY
    delivery state: its send is no longer the one the record is committed to."""
    await store.create_record(_record("m1"))
    now = time.time()
    assert await store.claim_delivery("m1", now, "worker-2", _LEASE) == 1

    if write == "provisional":
        assert await store.mark_provisional("m1", ["out-1"], 1, now, "worker-1") == -3
    elif write == "delivered":
        assert await store.mark_delivered("m1", ["out-1"], 1, now, "worker-1") == -3
    else:
        assert await store.mark_failed("m1", 1, now, "worker-1") == -3
    assert (await store.get_record("m1")).delivery_status is DeliveryStatus.PENDING_DELIVERY

    # The holder's own write goes through, and a lapsed foreign lease no longer blocks.
    assert await store.mark_provisional("m1", ["out-1"], 1, now, "worker-2") == 1


async def test_a_late_failed_write_cannot_undo_a_fully_sent_answer(store):
    """Once the answer is out the record is ``provisional``; the send path may no longer
    terminalise it ``failed``, or a hung worker's late refusal erases a delivered answer."""
    await store.create_record(_record("m1"))
    now = time.time()
    assert await store.mark_provisional("m1", ["out-1"], 1, now, "worker-2") == 1

    # The claim is released by the provisional write, so only the state guard stands here.
    assert await store.mark_failed("m1", 1, now, "worker-1") == -2
    assert (await store.get_record("m1")).delivery_status is DeliveryStatus.PROVISIONAL


# -- out-of-band receipt ingestion (conversations:record:receipt) -------------


async def test_a_receipt_confirms_a_provisional_record_once(store, lua_redis):
    await store.create_record(_record("m1"))
    now = time.time()
    await store.mark_provisional("m1", ["out-1"], 1, now, "tok")

    assert await store.ingest_receipt("m1", DeliveryReceipt.DELIVERED, now) == 1
    assert (await store.get_record("m1")).delivery_status is DeliveryStatus.DELIVERED
    # The grace-expiry fallback arriving after a receipt is a no-op, not a second write.
    assert await store.ingest_receipt("m1", DeliveryReceipt.DELIVERED, now) == 0
    # A failure receipt for a record already confirmed delivered is the conflict the
    # caller logs, never a silent overwrite.
    assert await store.ingest_receipt("m1", DeliveryReceipt.FAILED, now) == -2
    assert 0 < await lua_redis.ttl(_key("m1")) <= ConversationsSettings().answer_retention_ttl_seconds


async def test_a_receipt_reports_a_missing_record(store):
    assert await store.ingest_receipt("gone", DeliveryReceipt.DELIVERED, time.time()) == -1


@pytest.mark.parametrize("receipt", [DeliveryReceipt.DELIVERED, DeliveryReceipt.FAILED])
@pytest.mark.parametrize("status", [DeliveryStatus.ACCEPTED, DeliveryStatus.PENDING_DELIVERY])
async def test_a_receipt_before_the_send_finished_is_refused(store, lua_redis, status, receipt):
    """Chunk one's provider callback routinely lands while chunks two and three are still
    going out. Settling the record on it would terminalise a half-sent answer."""
    intake = "worker-1" if status is DeliveryStatus.ACCEPTED else None
    await store.create_record(_record("m1", status=status), intake_token=intake)
    now = time.time()

    assert await store.ingest_receipt("m1", receipt, now) == -3
    assert (await store.get_record("m1")).delivery_status is status
    # No terminal write means no retention TTL: the record is still live work.
    assert await lua_redis.ttl(_key("m1")) == -1


# -- the scans that drive the re-drive and the sweep --------------------------


async def test_the_scans_report_unfinished_work_and_skip_a_corrupt_row(store, lua_redis):
    await store.create_record(_record("m-pending"))
    await store.create_record(_record("m-intake", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    await store.create_record(_record("m-shed", status=DeliveryStatus.SHED))
    await store.create_record(_record("m-prov"))
    await store.mark_provisional("m-prov", ["out-1"], 1, time.time(), "tok")
    # A row corrupted under a live index entry — the listing reads the index, so this is
    # the shape a corrupt row reaches it in.
    await lua_redis.hset(_key("m-corrupt"), mapping={"delivery_status": "not-a-status", "attempts": "x"})
    await lua_redis.zadd(
        ConversationsSettings().status_index_key(DeliveryStatus.PENDING_DELIVERY.value), {"m-corrupt": float("inf")}
    )

    work = {item.message_id: item for item in await store.pending_work()}

    # The corrupt row is skipped and every other unfinished record still comes back.
    assert set(work) == {"m-pending", "m-prov"}
    assert work["m-prov"].grace_deadline is not None
    # The intake scan sees exactly the record whose turn never completed.
    intake = await store.list_by_status(frozenset({DeliveryStatus.ACCEPTED}))
    assert [record.message_id for record in intake] == ["m-intake"]


# -- the per-status index those listings read ---------------------------------


async def _indexed_under(lua_redis, message_id: str) -> list[str]:
    """Every status index naming ``message_id`` — exactly one, its current status."""
    settings = ConversationsSettings()
    return [
        status.value
        for status in DeliveryStatus
        if await lua_redis.zscore(settings.status_index_key(status.value), message_id) is not None
    ]


async def test_every_transition_moves_the_record_to_exactly_one_status_index(store, lua_redis):
    """The listings read the index rather than the keyspace, so a record left indexed under
    a status it has LEFT is picked up twice, and one indexed under none is invisible work."""
    await store.create_record(_record("m1", status=DeliveryStatus.ACCEPTED), intake_token="worker-1")
    assert await _indexed_under(lua_redis, "m1") == ["accepted"]

    assert await store.complete_turn(_record("m1")) == 1
    assert await _indexed_under(lua_redis, "m1") == ["pending_delivery"]
    # A live record's member outlives nothing: it is never swept out from under the listing.
    assert await lua_redis.zscore(ConversationsSettings().status_index_key("pending_delivery"), "m1") == float("inf")

    now = time.time()
    assert await store.mark_provisional("m1", ["out-1"], 1, now, "tok") == 1
    assert await _indexed_under(lua_redis, "m1") == ["provisional"]

    assert await store.ingest_receipt("m1", DeliveryReceipt.DELIVERED, now) == 1
    assert await _indexed_under(lua_redis, "m1") == ["delivered"]


@pytest.mark.parametrize("terminal", ["delivered", "failed"])
async def test_a_terminal_write_indexes_the_member_to_expire_with_its_row(store, lua_redis, terminal):
    await store.create_record(_record("m1"))
    now = time.time()
    if terminal == "delivered":
        assert await store.mark_delivered("m1", ["out-1"], 1, now, "tok") == 1
    else:
        assert await store.mark_failed("m1", 1, now, "tok") == 1

    assert await _indexed_under(lua_redis, "m1") == [terminal]
    # The member's score is the moment its row expires, so the index is swept with it and
    # never outlives what it names.
    score = await lua_redis.zscore(ConversationsSettings().status_index_key(terminal), "m1")
    assert score == pytest.approx(now + ConversationsSettings().answer_retention_ttl_seconds)
    assert await store.pending_work() == []


async def test_deleting_a_record_unindexes_it_in_the_same_step(store, lua_redis):
    """An index entry outliving its row hands every later listing a ``message_id`` with
    nothing behind it."""
    record = _record("m1", status=DeliveryStatus.ACCEPTED)
    await store.create_record(record, intake_token="worker-1")

    assert await store.delete_record(record) is True
    assert await lua_redis.exists(_key("m1")) == 0
    assert await _indexed_under(lua_redis, "m1") == []
    assert await store.list_by_status(frozenset({DeliveryStatus.ACCEPTED})) == []
    # A second delete removes nothing, and says so.
    assert await store.delete_record(record) is False


# -- the thread indexes (created, moved and unindexed by the same scripts) -----


async def test_creating_a_record_writes_both_thread_indexes(store, lua_redis):
    settings = ConversationsSettings()
    record = _record("m1")
    await store.create_record(record)

    assert await lua_redis.zscore(settings.thread_index_key("line", record.thread_id), "m1") == pytest.approx(
        record.created_at
    )
    assert await lua_redis.zscore(settings.route_threads_key("line"), record.thread_id) == pytest.approx(
        record.created_at
    )


async def test_a_create_landing_after_the_route_delete_writes_no_thread_index(store, lua_redis):
    """A door resolves its route a round trip before the record is created, so a delete
    completing in that window has already reclaimed both indexes. Writing them anyway
    re-creates a pair for a route that no longer routes: no read reaches it, no TTL expires
    it, and the prune (which walks LIVE routes only) never sees it again."""
    settings = ConversationsSettings()
    record = _record("m1")
    await lua_redis.delete(settings.route_key("line"))

    await store.create_record(record)

    # The record itself stands — its delivery is still the sender's to finish...
    stored = await store.get_record("m1")
    assert stored is not None
    assert await _indexed_under(lua_redis, "m1") == ["pending_delivery"]
    # ...but neither thread index was re-created.
    assert await lua_redis.exists(settings.thread_index_key("line", record.thread_id)) == 0
    assert await lua_redis.exists(settings.route_threads_key("line")) == 0


async def test_a_create_on_a_live_route_still_writes_both_thread_indexes(store, lua_redis):
    """The flip side of the guard: the ordinary create is what puts a message in its
    transcript, so a route that still routes must still be indexed."""
    settings = ConversationsSettings()
    record = _record("m1")

    await store.create_record(record)

    assert await lua_redis.zscore(settings.thread_index_key("line", record.thread_id), "m1") is not None
    assert await lua_redis.zscore(settings.route_threads_key("line"), record.thread_id) is not None


async def test_the_prune_step_reclaims_a_thread_whose_index_is_already_empty(store, lua_redis):
    """A thread with no member left to offer is still named by the route index. Nothing
    else drops it — the reads leave the index alone by design — so a pass that short-circuits
    on "no candidates" leaves it there forever: the route's ``total`` over-counts, the read
    doors re-log the orphan warning on every listing, and a door edit is blocked behind a
    count with no visible thread."""
    settings = ConversationsSettings()
    thread = "bridge:line:t"
    await store.create_record(_aged("m1", thread, time.time()))
    # The transcript index gone from under the route index — what an interrupted route
    # reclamation leaves behind (it deletes the thread's own index first).
    await lua_redis.delete(settings.thread_index_key("line", thread))
    assert await lua_redis.zscore(settings.route_threads_key("line"), thread) is not None

    await store.prune_expired_terminal_indexes(["line"])

    assert await lua_redis.zscore(settings.route_threads_key("line"), thread) is None


async def test_a_completed_turn_re_scores_its_thread_to_the_moment_it_landed(store, lua_redis):
    settings = ConversationsSettings()
    intake = _record("m1", status=DeliveryStatus.ACCEPTED)
    await store.create_record(intake, intake_token="worker-1")

    completed = ConversationRecord.model_validate(_record("m1").model_dump() | {"updated_at": intake.created_at + 42})
    assert await store.complete_turn(completed) == 1
    assert await lua_redis.zscore(settings.route_threads_key("line"), intake.thread_id) == pytest.approx(
        intake.created_at + 42
    )


async def test_the_thread_leaves_the_route_index_with_its_last_record(store, lua_redis):
    settings = ConversationsSettings()
    first = _record("m1")
    second = ConversationRecord.model_validate(_record("m2").model_dump() | {"thread_id": first.thread_id})
    await store.create_record(first)
    await store.create_record(second)

    assert await store.delete_record(first) is True
    assert await lua_redis.zscore(settings.route_threads_key("line"), first.thread_id) is not None

    assert await store.delete_record(second) is True
    assert await lua_redis.zcard(settings.thread_index_key("line", first.thread_id)) == 0
    assert await lua_redis.zscore(settings.route_threads_key("line"), first.thread_id) is None


def _aged(message_id: str, thread_id: str, created_at: float) -> ConversationRecord:
    """A record of ``thread_id`` created at a chosen moment. The thread index is scored by
    ``created_at``, so this is what makes a member a prune candidate or not."""
    return ConversationRecord.model_validate(
        _record(message_id).model_dump() | {"thread_id": thread_id, "created_at": created_at}
    )


async def test_the_prune_step_drops_expired_members_and_keeps_a_thread_that_still_has_one(store, lua_redis):
    settings = ConversationsSettings()
    aged = time.time() - settings.answer_retention_ttl_seconds - 60
    thread = "bridge:line:t"
    await store.create_record(_aged("m1", thread, aged))
    await store.create_record(_aged("m2", thread, aged + 1))
    # Only one row has been swept from under the index.
    await lua_redis.delete(_key("m1"))

    await store.prune_expired_terminal_indexes(["line"])

    assert await lua_redis.zrange(settings.thread_index_key("line", thread), 0, -1) == ["m2"]
    assert await lua_redis.zscore(settings.route_threads_key("line"), thread) is not None


async def test_the_prune_step_takes_the_thread_out_of_the_route_index_once_it_empties(store, lua_redis):
    settings = ConversationsSettings()
    aged = time.time() - settings.answer_retention_ttl_seconds - 60
    thread = "bridge:line:t"
    for index, message_id in enumerate(("m1", "m2")):
        await store.create_record(_aged(message_id, thread, aged + index))
        await lua_redis.delete(_key(message_id))

    await store.prune_expired_terminal_indexes(["line"])

    assert await lua_redis.zcard(settings.thread_index_key("line", thread)) == 0
    assert await lua_redis.zscore(settings.route_threads_key("line"), thread) is None


async def test_the_prune_step_leaves_a_member_that_is_not_yet_expired(store, lua_redis):
    # The score picks the candidates, the row decides: a member still inside the retention
    # window is never offered to the drop at all.
    settings = ConversationsSettings()
    thread = "bridge:line:t"
    await store.create_record(_aged("m1", thread, time.time()))
    await lua_redis.delete(_key("m1"))

    await store.prune_expired_terminal_indexes(["line"])

    assert await lua_redis.zrange(settings.thread_index_key("line", thread), 0, -1) == ["m1"]


@pytest.mark.parametrize("outcome", ["completed", "silent"])
async def test_a_turn_completing_after_a_route_delete_never_resurrects_the_route_index(store, lua_redis, outcome):
    """A route delete reclaims both thread indexes, and nothing walks the name afterwards.
    A completion that re-stamped the route index unconditionally would leave it holding a
    thread whose transcript index is empty — a pair no read reaches, no TTL expires, and the
    prune (which walks LIVE routes only) never sees."""
    settings = ConversationsSettings()
    intake = _record("m1", status=DeliveryStatus.ACCEPTED)
    await store.create_record(intake, intake_token="worker-1")
    await store.drop_route_threads("line")

    if outcome == "completed":
        assert await store.complete_turn(_record("m1")) == 1
    else:
        silent = ConversationRecord.model_validate(
            _record("m1").model_dump() | {"delivery_status": "silent", "answer_status": None, "answer": None}
        )
        assert await store.complete_silent(silent) == 1

    assert await lua_redis.exists(settings.route_threads_key("line")) == 0
    assert await lua_redis.exists(settings.thread_index_key("line", intake.thread_id)) == 0


async def test_a_completion_still_stamps_the_route_index_of_a_live_thread(store, lua_redis):
    """The flip side of the guard: the ordinary completion is what moves a thread to the top
    of the operator listing, so it must still stamp a thread that still has members."""
    settings = ConversationsSettings()
    intake = _record("m1", status=DeliveryStatus.ACCEPTED)
    await store.create_record(intake, intake_token="worker-1")

    completed = ConversationRecord.model_validate(_record("m1").model_dump() | {"updated_at": intake.created_at + 42})
    assert await store.complete_turn(completed) == 1
    assert await lua_redis.zscore(settings.route_threads_key("line"), intake.thread_id) == pytest.approx(
        intake.created_at + 42
    )


async def test_the_prune_step_reports_what_the_thread_index_still_holds(store, lua_redis):
    """The walker charges its budget per thread and advances a rank cursor, so it needs the
    script to say whether this thread just LEFT the route index and shifted the threads
    behind it — a count of removals alone cannot answer that."""
    settings = ConversationsSettings()
    aged = time.time() - settings.answer_retention_ttl_seconds - 60
    thread = "bridge:line:t"
    await store.create_record(_aged("m1", thread, aged))
    await store.create_record(_aged("m2", thread, aged + 1))
    await lua_redis.delete(_key("m1"))

    step = await store._prune_thread(
        lua_redis, "line", thread, time.time() - settings.answer_retention_ttl_seconds, start_rank=0, budget=10
    )

    assert step.spent == 2
    # One member removed, one still standing — so the thread has not left the route index.
    assert step.emptied is False
    assert await lua_redis.zscore(settings.route_threads_key("line"), thread) is not None


async def test_dropping_a_route_deletes_its_thread_indexes(store, lua_redis):
    settings = ConversationsSettings()
    await store.create_record(_aged("m1", "bridge:line:a", time.time()))
    await store.create_record(_aged("m2", "bridge:line:b", time.time()))

    await store.drop_route_threads("line")

    assert await lua_redis.exists(settings.route_threads_key("line")) == 0
    assert await lua_redis.exists(settings.thread_index_key("line", "bridge:line:a")) == 0
    assert await lua_redis.exists(settings.thread_index_key("line", "bridge:line:b")) == 0


async def test_the_orphan_unindex_drops_a_member_whose_row_is_gone(store, lua_redis):
    # The row swept from under the status index: the listing that finds it must unindex it
    # rather than hand a caller a message_id with nothing behind it.
    await store.create_record(_record("m1"))
    await lua_redis.delete(_key("m1"))

    assert await store.list_by_status(frozenset({DeliveryStatus.PENDING_DELIVERY})) == []
    assert await _indexed_under(lua_redis, "m1") == []


async def test_the_largest_served_page_is_a_rank_redis_will_take(store, lua_redis):
    # Why the paging guard has an UPPER bound: an offset past redis's signed-64 rank range
    # is a ResponseError, which is not a typed operation error and would escape the door as
    # an unmapped 500. The largest window the door serves must sit inside that range.
    from redis.exceptions import ResponseError

    from tai42_skeleton.operations.conversations import MAX_THREAD_PAGE, MAX_THREAD_PAGE_SIZE

    thread = "bridge:line:t"
    await store.create_record(_aged("m1", thread, time.time()))

    largest = await store.list_thread_records(
        "line", thread, offset=(MAX_THREAD_PAGE - 1) * MAX_THREAD_PAGE_SIZE, limit=MAX_THREAD_PAGE_SIZE
    )
    assert largest.records == []
    assert largest.total == 1

    with pytest.raises(ResponseError):
        await store.list_thread_records("line", thread, offset=2**63, limit=MAX_THREAD_PAGE_SIZE)
