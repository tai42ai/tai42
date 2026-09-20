"""The reaper's continuation-due redelivery: the ``due_continuations`` scan, the
``claim_continuation_retry`` backoff (idempotent within a window), orphan
reconciliation (``CONTINUATION_DROPPED``) with the loud terminal give-up + the
abandonment-handler fire, and per-pass resilience to a poison member.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.store import CONTINUATION_DROPPED
from tai42_skeleton.operations import interactions as ops

from ._continuation_support import async_req, configure_interactions_store, drain, make_wired, seed_due


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


async def test_crash_before_run_tool_is_redelivered_by_reaper(wired, monkeypatch, caplog):
    # A worker crash AFTER the answer is claimed but BEFORE ``run_tool`` returns must
    # not lose the resume: the durable record survives the failed fire and the reaper
    # redelivers it until ``run_tool`` returns, firing exactly once from the consumer's
    # (idempotent) view. The crash is surfaced loudly, never swallowed.
    settings = wired.settings.model_copy(update={"expiry_reaper_interval_seconds": 0.01})
    # The door computes the due-record's first-attempt from ITS settings, so the record
    # is near-due immediately; the reaper reads the same tiny interval for redelivery.
    monkeypatch.setattr(ops, "interactions_settings", lambda: settings)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(reaper_module, "interactions_settings", lambda: settings)

    applied: list[Any] = []
    attempts = {"n": 0}

    async def _flaky(identity, fingerprint, tool, interaction_id, answer, park_context=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("worker crashed mid-resume")
        applied.append({"identity": identity, "tool": tool, "answer": answer})

    monkeypatch.setattr(continuation_module, "_run_continuation", _flaky)
    await wired.store.add(wired.fake, async_req(wired.store, iid="d1"), idle_ttl=86400, continuation_fingerprint="fp-1")
    caplog.set_level(logging.ERROR)
    assert await ops.answer_interaction("d1", "go") == {"interaction_id": "d1", "status": "answered"}
    await drain()
    # The first fire crashed: nothing applied, the record was NOT cleared.
    assert applied == []
    assert attempts["n"] == 1
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == ["d1"]
    assert any("continuation" in rec.message and "failed" in rec.message for rec in caplog.records)

    # Past the first-attempt window, the reaper redelivers — this fire returns.
    await asyncio.sleep(0.03)
    assert await reaper_module.redeliver_due_continuations_once() == 1
    await drain()
    assert len(applied) == 1
    assert applied[0] == {"identity": "svc-key", "tool": "resume_tool", "answer": "go"}

    # The record is cleared; a further pass redelivers nothing (exactly once applied).
    assert await reaper_module.redeliver_due_continuations_once() == 0
    await drain()
    assert len(applied) == 1
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == []


async def test_redelivery_claim_is_idempotent_within_a_backoff_window(wired):
    # Two racing redelivery claims on the SAME due record within one backoff window
    # yield exactly one re-fire: the first advances the record's next-attempt score and
    # returns it, the second sees it no longer due and declines. At-least-once never
    # becomes a storm; consumer idempotency covers the rare cross-window double-fire.
    now = datetime.now(UTC)
    past_ms = int(now.timestamp() * 1000) - 1000
    await seed_due(wired.store, wired.fake, "i1", answer={"a": 1}, first_attempt_at_ms=past_ms)
    base_ms, cap_ms = 30_000, 86_400_000
    first = await wired.store.claim_continuation_retry(wired.fake, "i1", now, base_ms, cap_ms)
    second = await wired.store.claim_continuation_retry(wired.fake, "i1", now, base_ms, cap_ms)
    assert first is not None
    assert first.tool == "resume_tool"
    assert first.identity == "svc-key"
    assert first.fingerprint == "fp-1"
    assert first.answer == {"a": 1}
    assert first.attempts == 1
    assert second is None  # not due again until the backoff elapses


async def test_redelivery_reconciles_orphan_index_member(wired):
    # A due-index member whose record hash has TTL-expired is an orphan: the retry
    # claim reconciles it off the index and fires nothing — never a phantom redelivery.
    # It reports the terminal drop (CONTINUATION_DROPPED), distinct from a benign
    # not-due None, so the reaper can surface a loud give-up.
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    await wired.fake.zadd(wired.store.continuation_due_index_key, {"ghost": now_ms - 1000})
    claimed = await wired.store.claim_continuation_retry(wired.fake, "ghost", now, 30_000, 86_400_000)
    assert claimed is CONTINUATION_DROPPED
    assert await wired.store.due_continuations(wired.fake, now + timedelta(hours=1)) == []


async def test_redelivery_pass_logs_loud_terminal_drop(wired, caplog):
    # When a due record's retention horizon has lapsed (its hash TTL-expired, leaving an
    # orphan index member), the redelivery pass reconciles it AND emits a LOUD terminal
    # give-up — never 24h of errors then silence. It fires nothing and returns 0.
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    await wired.fake.zadd(wired.store.continuation_due_index_key, {"ghost": now_ms - 1000})
    caplog.set_level(logging.ERROR)
    assert await reaper_module.redeliver_due_continuations_once() == 0
    assert any("ghost" in rec.message and "permanently dropped" in rec.message for rec in caplog.records)
    assert await wired.store.due_continuations(wired.fake, now + timedelta(hours=1)) == []


async def test_redelivery_drop_fires_the_continuation_abandonment_handler(wired, monkeypatch):
    # The terminal give-up is not just LOGGED — it notifies every registered
    # continuation-abandonment handler by interaction id, so a resuming driver can close the
    # tail (fire its own non-success completion) instead of leaving the bound caller waiting to
    # its own deadline. Fired exactly once, after the drop is reconciled.
    from tai42_contract.interactions import continuation as contract_cont

    seen: list[str] = []

    async def _handler(interaction_id: str) -> None:
        seen.append(interaction_id)

    monkeypatch.setattr(contract_cont, "_continuation_abandonment_handlers", [_handler])

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    # An orphan index member whose record hash has TTL-expired: the retry claim reports the
    # permanent drop, and the reaper fires the abandonment notice for it.
    await wired.fake.zadd(wired.store.continuation_due_index_key, {"ghost": now_ms - 1000})
    assert await reaper_module.redeliver_due_continuations_once() == 0
    assert seen == ["ghost"]


async def test_redelivery_drop_survives_a_raising_abandonment_handler(wired, monkeypatch, caplog):
    # A handler that raises must never abort the reaper pass or the drop reconciliation: it is
    # logged and swallowed, the healthy handler still fires, and the pass completes.
    from tai42_contract.interactions import continuation as contract_cont

    seen: list[str] = []

    async def _poison(interaction_id: str) -> None:
        raise RuntimeError("handler boom")

    async def _healthy(interaction_id: str) -> None:
        seen.append(interaction_id)

    monkeypatch.setattr(contract_cont, "_continuation_abandonment_handlers", [_poison, _healthy])

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    await wired.fake.zadd(wired.store.continuation_due_index_key, {"ghost": now_ms - 1000})
    caplog.set_level(logging.WARNING)
    assert await reaper_module.redeliver_due_continuations_once() == 0
    assert seen == ["ghost"]
    assert any("abandonment handler raised" in rec.message for rec in caplog.records)


async def test_redelivery_pass_survives_a_poison_member(wired, monkeypatch, caplog):
    # One member whose claim deterministically raises must not abort the whole pass and
    # starve the healthy members: the poison member is logged loudly and skipped, and a
    # co-due healthy record still redelivers in the same pass.
    now = datetime.now(UTC)
    past_ms = int(now.timestamp() * 1000) - 1000
    await seed_due(wired.store, wired.fake, "poison", answer={"p": 1}, first_attempt_at_ms=past_ms)
    await seed_due(wired.store, wired.fake, "healthy", answer={"h": 1}, first_attempt_at_ms=past_ms)

    real_claim = wired.store.claim_continuation_retry

    async def _claim(r, interaction_id, *a, **k):
        if interaction_id == "poison":
            raise RuntimeError("poison member")
        return await real_claim(r, interaction_id, *a, **k)

    monkeypatch.setattr(wired.store, "claim_continuation_retry", _claim)
    monkeypatch.setattr(reaper_module, "InteractionStore", lambda _prefix: wired.store)

    fired: list[str] = []
    monkeypatch.setattr(reaper_module, "redeliver_continuation", lambda store, due: fired.append(due.interaction_id))
    caplog.set_level(logging.ERROR)
    assert await reaper_module.redeliver_due_continuations_once() == 1
    assert fired == ["healthy"]
    assert any("poison" in rec.message and "skipping it this pass" in rec.message for rec in caplog.records)
