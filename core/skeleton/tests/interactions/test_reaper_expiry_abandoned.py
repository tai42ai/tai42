"""The expiry reaper's loud terminus for a park it can no longer resolve.

Every resolution of an async park (an answer claim, a kill prune, a cancel prune) removes
its expiry-index member in the same MULTI that ends the state. So a due member that is
still indexed when the reaper finds no pending park behind it lapsed unresolved: the reaper
logs it at ERROR once, emits ``interactions_ask_expiry_abandoned`` once with
``{interaction_id, expiry_at}``, and reconciles the member off. A resolution that raced the
reaper's scan already removed the member, and stays quiet.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.operations import interactions as ops

from ._continuation_support import (
    RecordingHooks,
    async_req,
    configure_interactions_store,
    drain,
    make_captured,
    make_wired,
)

_ABANDONED = "interactions_ask_expiry_abandoned"


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    wired = make_wired(monkeypatch, fake_redis, fake_client_ctx)
    monkeypatch.setattr(reaper_module, "InteractionStore", lambda _prefix: wired.store)
    return wired


@pytest.fixture
def captured(monkeypatch):
    return make_captured(monkeypatch)


@pytest.fixture
def hooks(monkeypatch):
    from tai42_skeleton.hooks import cache as hooks_cache

    recording = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: recording)
    return recording


def _abandoned(hooks: RecordingHooks) -> list[dict]:
    return [event.payload for event in hooks.events if event.topic == _ABANDONED]


def _abandon_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec for rec in caplog.records if rec.levelno == logging.ERROR and "permanently abandoned" in rec.getMessage()
    ]


def _lapse_after_reading(monkeypatch, wired, interaction_id: str) -> None:
    """The park's state hash lapses right after the reaper reads it, before its kill or claim."""
    real_get_state = wired.store.get_state

    async def _get_state(r, iid):
        state = await real_get_state(r, iid)
        if iid == interaction_id:
            await wired.fake.delete(wired.store.state_key(iid))
        return state

    monkeypatch.setattr(wired.store, "get_state", _get_state)


async def test_a_due_member_whose_park_lapsed_is_surfaced_once_and_reconciled(wired, captured, hooks, caplog):
    caplog.set_level(logging.ERROR)
    expiry_ms = int(datetime.now(UTC).timestamp() * 1000) - 1000
    await wired.fake.zadd(wired.store.pending_expiry_key, {"lapsed-a": expiry_ms, "lapsed-b": expiry_ms + 1})

    assert await reaper_module.reap_expired_parks_once() == 0
    await drain()

    expected_at = datetime.fromtimestamp(expiry_ms / 1000, UTC)
    assert _abandoned(hooks) == [
        {"interaction_id": "lapsed-a", "expiry_at": expected_at.isoformat()},
        {"interaction_id": "lapsed-b", "expiry_at": (expected_at + timedelta(milliseconds=1)).isoformat()},
    ]
    errors = _abandon_errors(caplog)
    assert [("lapsed-a" in rec.getMessage(), "lapsed-b" in rec.getMessage()) for rec in errors] == [
        (True, False),
        (False, True),
    ]
    assert captured == []
    assert await wired.store.due_expiries(wired.fake, datetime.now(UTC)) == []

    # The member was reconciled: a later pass has nothing to surface.
    caplog.clear()
    assert await reaper_module.reap_expired_parks_once() == 0
    assert len(_abandoned(hooks)) == 2
    assert _abandon_errors(caplog) == []


async def test_a_park_resolved_between_the_scan_and_the_read_stays_quiet(wired, captured, hooks, caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(wired.fake, async_req(wired.store, iid="c1", expiry_at=past), idle_ttl=86400)
    listed = await wired.store.due_expiries(wired.fake, datetime.now(UTC))
    assert listed == ["c1"]
    # A door's cancel prunes the park after the reaper listed it.
    assert await wired.store.prune_pending(wired.fake, "c1", "ag", reason="cancelled") == "pruned"

    async def _due(_r, _now):
        return listed

    monkeypatch.setattr(wired.store, "due_expiries", _due)

    assert await reaper_module.reap_expired_parks_once() == 0
    assert _abandoned(hooks) == []
    assert _abandon_errors(caplog) == []


async def test_an_expiry_kill_that_finds_the_park_gone_surfaces_it_in_the_same_pass(
    wired, captured, hooks, caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    past = datetime.now(UTC) - timedelta(seconds=1)
    request = async_req(wired.store, iid="k1", expiry_at=past, on_expiry="kill")
    await wired.store.add(wired.fake, request, idle_ttl=86400, run_delivery_id="rd-k1")
    _lapse_after_reading(monkeypatch, wired, "k1")

    assert await reaper_module.reap_expired_parks_once() == 0

    assert [payload["interaction_id"] for payload in _abandoned(hooks)] == ["k1"]
    assert len(_abandon_errors(caplog)) == 1
    assert await wired.store.due_expiries(wired.fake, datetime.now(UTC)) == []
    assert await wired.fake.hgetall(wired.store.kill_due_key("k1")) == {}


async def test_an_expiry_claim_that_finds_the_park_gone_surfaces_it_in_the_same_pass(
    wired, captured, hooks, caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="r1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp"
    )
    _lapse_after_reading(monkeypatch, wired, "r1")

    assert await reaper_module.reap_expired_parks_once() == 0
    await drain()

    assert captured == []
    assert [payload["interaction_id"] for payload in _abandoned(hooks)] == ["r1"]
    assert len(_abandon_errors(caplog)) == 1
    assert await wired.store.due_expiries(wired.fake, datetime.now(UTC)) == []


async def test_an_expiry_claim_that_loses_to_an_answer_stays_quiet(wired, captured, hooks, caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="a1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp"
    )
    real_get_state = wired.store.get_state

    async def _get_state(r, iid):
        state = await real_get_state(r, iid)
        # A human answer claims the park after the reaper read it pending.
        assert await ops.answer_interaction(iid, "human") == {"interaction_id": iid, "status": "answered"}
        return state

    monkeypatch.setattr(wired.store, "get_state", _get_state)

    assert await reaper_module.reap_expired_parks_once() == 0
    await drain()

    assert [call["answer"] for call in captured] == ["human"]
    assert _abandoned(hooks) == []
    assert _abandon_errors(caplog) == []
