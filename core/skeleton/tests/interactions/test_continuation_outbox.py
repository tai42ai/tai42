"""The durable continuation-due enqueue committed in the SAME MULTI as the answer
claim: ``record_answer`` writes the outbox record atomically with the ``answered``
state for an async park, writes none for a sync question, and raises loudly when an
async park is resolved without the continuation-due timing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse

from ._continuation_support import async_req, configure_interactions_store, make_wired


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


async def test_record_answer_enqueues_due_record_atomically_with_claim(wired):
    # The transactional-outbox invariant: the durable due-record is written in the SAME
    # MULTI as the claim, so the instant record_answer returns True — BEFORE any fire —
    # the answered state AND the due-record both exist. A crash right after the claim
    # can never leave a claimed answer with no due-record.
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="atom1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    response = InteractionResponse(interaction_id="atom1", answer="go", answered_by="u", answered_at=datetime.now(UTC))
    claimed = await wired.store.record_answer(
        wired.fake, response, "ag", 60, continuation_due_ttl=86400, continuation_first_attempt_at_ms=now_ms + 30_000
    )
    assert claimed is True
    # NO fire ran (record_answer only claims + enqueues); the outbox record already exists.
    state = await wired.store.get_state(wired.fake, "atom1")
    assert state is not None
    assert state.status == "answered"
    raw = await wired.fake.hgetall(wired.store.continuation_due_key("atom1"))
    assert raw["tool"] == "resume_tool"
    assert raw["identity"] == "svc-key"
    assert raw["fingerprint"] == "fp-1"
    assert json.loads(raw["answer"]) == "go"
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == ["atom1"]


async def test_record_answer_sync_writes_no_due_record(wired):
    # A sync question carries no continuation — the claim writes NO due-record (behavior
    # unchanged), even when the caller passes timing.
    now = datetime.now(UTC)
    sync_req = InteractionRequest(
        interaction_id="sy1",
        group_id="sg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("sy1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )
    await wired.store.add(wired.fake, sync_req, idle_ttl=86400)
    response = InteractionResponse(interaction_id="sy1", answer="hi", answered_by="u", answered_at=now)
    claimed = await wired.store.record_answer(
        wired.fake,
        response,
        "sg",
        60,
        continuation_due_ttl=86400,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 30_000,
    )
    assert claimed is True
    assert await wired.fake.hgetall(wired.store.continuation_due_key("sy1")) == {}
    assert await wired.store.due_continuations(wired.fake, now + timedelta(hours=1)) == []


async def test_record_answer_async_without_timing_raises(wired):
    # An async park resolved without the continuation-due timing is a caller bug — it
    # raises loudly, never silently skipping the durable enqueue (never-silent-error).
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="nt1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    response = InteractionResponse(interaction_id="nt1", answer="go", answered_by="u", answered_at=datetime.now(UTC))
    with pytest.raises(RuntimeError, match="continuation-due timing"):
        await wired.store.record_answer(wired.fake, response, "ag", 60)
