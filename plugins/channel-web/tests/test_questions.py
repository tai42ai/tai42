"""Pending-question records — reserve/peek/claim/release, the remaining-TTL restore,
its no-clobber SET NX, and the restore cap."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tai42_channel_web.store.questions import (
    QuestionRecord,
    claim_question,
    peek_question,
    release_question,
    reserve_question,
    restore_question,
)

from .conftest import CALLBACK, IDENTITY, VISITOR_ID, FakeRedis, _deadline

pytestmark = pytest.mark.usefixtures("web_env")

_QUESTION_KEY = "channel:web:question:int-1"


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
