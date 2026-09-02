"""The async park's continuation dispatch: both answer doors and the expiry reaper
funnel through the single post-claim seam, so a resolved park fires its stored
continuation EXACTLY ONCE, bound to the STORED identity (never the answerer's),
carrying ``{interaction_id, answer}``. The reaper resolves an expired park by the
generic EXPIRY answer and prunes it, and an answer racing an expiry yields exactly
one continuation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from tai42_contract.access_control import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.continuation import EXPIRY_ANSWER
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import CONTINUATION_DROPPED
from tai42_skeleton.operations import interactions as ops

from .._fakes.interactions_redis import FakeRedis


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    settings = InteractionsSettings()
    monkeypatch.setattr(ops, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(ops, "interactions_settings", lambda: settings)
    monkeypatch.setattr(reaper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(reaper_module, "interactions_settings", lambda: settings)
    # The detached fire's own client (the clear of the durable due-record) opens
    # through the continuation module's seam — point it at the same fake.
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis)


@pytest.fixture
def captured(monkeypatch):
    # Capture the detached continuation's rebind + dispatch args WITHOUT running the
    # real execution-identity bind / run_tool machinery.
    calls: list[dict] = []

    async def _stub(identity, fingerprint, tool, interaction_id, answer):
        calls.append(
            {
                "identity": identity,
                "fingerprint": fingerprint,
                "tool": tool,
                "interaction_id": interaction_id,
                "answer": answer,
            }
        )

    monkeypatch.setattr(continuation_module, "_run_continuation", _stub)
    return calls


def _async_req(store: InteractionStore, *, iid: str, gid: str = "ag", expiry_at: datetime | None = None):
    now = datetime.now(UTC)
    deadline = expiry_at or now + timedelta(hours=1)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=deadline,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=deadline,
    )


async def _drain() -> None:
    # Let the detached continuation task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _seed_due(store, fake, iid, *, answer, first_attempt_at_ms):
    # Seed a durable continuation-due record + its index member directly (the shape the
    # atomic claim writes), for redelivery tests that drive the reaper machinery without
    # a full answer round trip.
    await fake.hset(
        store.continuation_due_key(iid),
        mapping={
            "tool": "resume_tool",
            "identity": "svc-key",
            "fingerprint": "fp-1",
            "answer": json.dumps(answer),
            "attempts": "0",
        },
    )
    await fake.zadd(store.continuation_due_index_key, {iid: first_attempt_at_ms})


async def test_answer_door_fires_continuation_under_stored_identity(wired, captured):
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="a1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    # Answerer is a DIFFERENT identity than the stored continuation identity.
    token = set_request_user_id("answerer-x")
    try:
        assert await ops.answer_interaction("a1", "yes") == {"interaction_id": "a1", "status": "answered"}
    finally:
        reset_request_user_id(token)
    await _drain()
    assert len(captured) == 1
    call = captured[0]
    assert call["identity"] == "svc-key"  # the STORED identity, never "answerer-x"
    assert call["fingerprint"] == "fp-1"
    assert call["tool"] == "resume_tool"
    assert call["interaction_id"] == "a1"
    assert call["answer"] == "yes"


async def test_sync_answer_fires_no_continuation(wired, captured):
    now = datetime.now(UTC)
    sync_req = InteractionRequest(
        interaction_id="s1",
        group_id="sg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("s1"),
        created_at=now,
        timeout_at=now + timedelta(seconds=60),
    )
    await wired.store.add(wired.fake, sync_req, idle_ttl=86400)
    assert await ops.answer_interaction("s1", "hello") == {"interaction_id": "s1", "status": "answered"}
    await _drain()
    assert captured == []


async def test_reaper_fires_expiry_continuation_once_then_prunes(wired, captured):
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="e1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1
    await _drain()
    assert len(captured) == 1
    assert captured[0]["identity"] == "svc-key"
    assert captured[0]["fingerprint"] == "fp-1"
    assert captured[0]["answer"] == EXPIRY_ANSWER

    # Pruned from the expiry index and marked answered.
    assert await wired.store.due_expiries(wired.fake, datetime.now(UTC)) == []
    state = await wired.store.get_state(wired.fake, "e1")
    assert state is not None
    assert state.status == "answered"
    # A second pass fires nothing.
    assert await reaper_module.reap_expired_parks_once() == 0
    await _drain()
    assert len(captured) == 1


async def test_answer_racing_expiry_yields_exactly_one_continuation(wired, captured):
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="r1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    # A human answer lands first and claims the park.
    assert await ops.answer_interaction("r1", "human") == {"interaction_id": "r1", "status": "answered"}
    await _drain()
    assert len(captured) == 1
    assert captured[0]["answer"] == "human"
    # The reaper then finds it already answered: it claims nothing and fires nothing.
    assert await reaper_module.reap_expired_parks_once() == 0
    await _drain()
    assert len(captured) == 1


class RecordingHooks:
    """Captures every ``on_event`` the reaper emits."""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None) -> None:
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


async def test_reaper_emits_ask_expired_unanswered_with_payload(wired, captured, monkeypatch):
    # A park claimed by expiry STATES the fact as exactly one
    # ``interactions_ask_expired_unanswered`` event carrying the interaction/group ids,
    # the delivery channel + recipient, and an ISO8601 UTC ``expired_at`` — alongside
    # the continuation the reaper still fires.
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    past = datetime.now(UTC) - timedelta(seconds=1)
    req = InteractionRequest(
        interaction_id="x1",
        group_id="xg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("x1"),
        created_at=datetime.now(UTC),
        timeout_at=past,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=past,
        channel="telegram",
        recipient="@ops",
    )
    await wired.store.add(wired.fake, req, idle_ttl=86400, continuation_fingerprint="fp-1")

    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1
    await _drain()

    assert len(captured) == 1  # the expiry continuation still fired
    assert len(hooks.events) == 1
    event = hooks.events[0]
    assert event.topic == reaper_module.ASK_EXPIRED_UNANSWERED_EVENT_TOPIC == "interactions_ask_expired_unanswered"
    assert event.payload["interaction_id"] == "x1"
    assert event.payload["group_id"] == "xg"
    assert event.payload["channel"] == "telegram"
    assert event.payload["recipient"] == "@ops"
    expired_at = datetime.fromisoformat(event.payload["expired_at"])
    assert expired_at.tzinfo is not None  # an aware ISO8601 UTC timestamp


async def test_reaper_emits_null_channel_recipient_when_unset(wired, captured, monkeypatch):
    # A studio-inbox-only park (no channel/recipient) still emits — the two fields ride
    # as ``None`` rather than being dropped.
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="n1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )

    assert await reaper_module.reap_expired_parks_once() == 1
    await _drain()

    assert len(hooks.events) == 1
    assert hooks.events[0].payload["channel"] is None
    assert hooks.events[0].payload["recipient"] is None


async def test_reaper_hooks_failure_does_not_break_continuation_fire(wired, captured, monkeypatch):
    # A hooks-manager failure on the expiry event is swallowed: the reaper still claims
    # the park and fires its continuation, and the pass reports the fire.
    from tai42_skeleton.hooks import cache as hooks_cache

    class BoomHooks:
        async def on_event(self, topic, payload, *, tool_kwargs_override=None):
            raise RuntimeError("hooks down")

    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: BoomHooks())
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="h1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )

    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1  # the pass did not abort on the hooks failure
    await _drain()
    assert len(captured) == 1  # the continuation still fired despite the hooks failure


async def test_reaper_drops_stale_index_member_for_vanished_state(wired, captured, monkeypatch):
    # An expiry member whose state has vanished (idle-expired) is reconciled off the
    # index without firing — never a phantom continuation, and never a phantom
    # ``ask_expired_unanswered`` event either: the event states a successful expiry
    # CLAIM, and a vanished member exits before ever claiming. (The lost-race exit —
    # ``claimed is False`` — shares the same ``if claimed:`` guard by structure.)
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await wired.fake.zadd(wired.store.pending_expiry_key, {"ghost": now_ms - 1000})
    assert await reaper_module.reap_expired_parks_once() == 0
    await _drain()
    assert captured == []
    assert hooks.events == []
    assert await wired.store.due_expiries(wired.fake, datetime.now(UTC)) == []


async def test_park_state_survives_past_idle_ttl_so_reaper_can_fire(wired, captured, monkeypatch):
    # A park whose ``expiry_at`` runs past ``idle_ttl``: its state hash must survive to
    # the expiry (plus a reaper-pass margin), or the reaper would read ``state=None``
    # and drop the expiry member WITHOUT firing — a silent strand — and the question
    # would be unanswerable in the ``idle_ttl``..``expiry_at`` gap. With a flat
    # ``idle_ttl`` the state expires at fake-time 100 and the reaper fires nothing.
    idle_ttl = 100
    base = datetime.now(UTC)
    expiry = base + timedelta(seconds=300)  # well beyond idle_ttl
    await wired.store.add(
        wired.fake,
        _async_req(wired.store, iid="p1", expiry_at=expiry),
        idle_ttl=idle_ttl,
        continuation_fingerprint="fp-1",
        expiry_ttl_margin_seconds=10,
    )
    # Advance the store clock past idle_ttl but before the park horizon+margin (310):
    # the state (and its count) are still live — answerable in the old gap.
    wired.fake.advance(150)
    state = await wired.store.get_state(wired.fake, "p1")
    assert state is not None
    assert state.status == "pending"

    # The reaper runs once wall-time passes the expiry; the state is still present, so
    # it claims and fires exactly once.
    class _Clock:
        @staticmethod
        def now(tz=None):
            return expiry + timedelta(seconds=1)

    monkeypatch.setattr(reaper_module, "datetime", _Clock)
    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1
    await _drain()
    assert len(captured) == 1
    assert captured[0]["identity"] == "svc-key"
    assert captured[0]["answer"] == EXPIRY_ANSWER

    # And the state does eventually expire — the TTL is finite, not leaked forever.
    wired.fake.advance(200)  # store clock now 350 > horizon 310
    assert await wired.store.get_state(wired.fake, "p1") is None


async def test_run_continuation_runs_tool_under_stored_identity(wired, monkeypatch):
    # The REAL ``_run_continuation`` (not the stub): it binds the STORED continuation
    # identity via ``bind_execution_identity`` and then calls ``run_tool`` — the tool
    # must see the stored identity, never the answerer's, and its result path works.
    # Local imports: pulling the access_control package at module top reorders the
    # import graph into a circular init.
    from tai42_skeleton.access_control.settings import AccessControlSettings
    from tai42_skeleton.authz import execution as execution_module
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=False))

    seen: list[dict] = []

    async def _fake_run_tool(tool, arguments):
        identity = get_execution_identity()
        seen.append({"tool": tool, "arguments": arguments, "user_id": identity.user_id if identity else None})
        return {"ran": True}

    # ``run_tool`` is the platform tool-registry seam the real continuation dispatches
    # through; fake only it, so the REAL ``bind_execution_identity`` + detached-task
    # path still run.
    fake_app = SimpleNamespace(tools=SimpleNamespace(run_tool=_fake_run_tool))
    monkeypatch.setattr(continuation_module, "tai42_app", fake_app)

    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="rc1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    token = set_request_user_id("answerer-x")  # a DIFFERENT identity resolves the park
    try:
        assert await ops.answer_interaction("rc1", "go") == {"interaction_id": "rc1", "status": "answered"}
    finally:
        reset_request_user_id(token)
    await _drain()
    assert len(seen) == 1
    assert seen[0]["tool"] == "resume_tool"
    assert seen[0]["arguments"] == {"interaction_id": "rc1", "answer": "go"}
    assert seen[0]["user_id"] == "svc-key"  # the STORED identity, never "answerer-x"


async def test_run_continuation_deposits_resume_origin_for_lifecycle_correlation(wired, monkeypatch):
    # The runs-index correlation seam: the REAL continuation drive deposits the parked
    # interaction's id (the ambient ``resume_origin``) around its ``run_tool``
    # re-entry, so the resume dispatch's runs-index row can name the lifecycle it
    # continues — and the deposit is scoped to the fire, reset afterwards.
    from tai42_skeleton.access_control.settings import AccessControlSettings
    from tai42_skeleton.authz import execution as execution_module
    from tai42_skeleton.runs.chokepoint import get_resume_origin

    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=False))

    seen: list[str | None] = []

    async def _fake_run_tool(tool, arguments):
        seen.append(get_resume_origin())
        return {"ran": True}

    fake_app = SimpleNamespace(tools=SimpleNamespace(run_tool=_fake_run_tool))
    monkeypatch.setattr(continuation_module, "tai42_app", fake_app)

    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="ro1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    token = set_request_user_id("answerer-x")
    try:
        assert await ops.answer_interaction("ro1", "go") == {"interaction_id": "ro1", "status": "answered"}
    finally:
        reset_request_user_id(token)
    await _drain()
    assert seen == ["ro1"]  # the run_tool re-entry saw the parked interaction's id
    assert get_resume_origin() is None  # nothing leaks outside the detached fire


async def test_dispatch_continuation_retains_task_until_done(wired, monkeypatch):
    # The detached continuation task is held by a strong reference while in flight, so
    # it can never be GC-collected mid-resume; the reference is dropped when it ends.
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(identity, fingerprint, tool, interaction_id, answer):
        started.set()
        await release.wait()

    monkeypatch.setattr(continuation_module, "_run_continuation", _slow)

    continuation_module.dispatch_continuation(wired.store, _async_req(wired.store, iid="gc1"), "fp-1", "answer")
    await started.wait()
    assert any(t.get_name() == "interaction-continuation-gc1" for t in continuation_module._CONTINUATION_TASKS)
    release.set()
    await _drain()
    assert not any(t.get_name() == "interaction-continuation-gc1" for t in continuation_module._CONTINUATION_TASKS)


class _EvalBarrierRedis(FakeRedis):
    """A fake whose ``eval`` — the phantom-purge call every ``add`` makes AFTER its
    pre-write reads and BEFORE it commits — is a two-party rendezvous: the first
    ``add`` to reach it suspends until the second arrives, so both parks are past
    every read before either writes. That is the exact interleave the concurrent
    same-group TTL race needs: the last committer must not have seen the other
    park's write."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = 0
        self._both_in = asyncio.Event()

    async def eval(self, script, numkeys, *keys_and_args):
        self.arrived += 1
        if self.arrived >= 2:
            self._both_in.set()
        else:
            await self._both_in.wait()
        return await super().eval(script, numkeys, *keys_and_args)


async def test_concurrent_same_group_parks_never_shrink_shared_ttls(monkeypatch, captured):
    # Two async parks into the SAME group with divergent expiries (long + short),
    # interleaved so BOTH read the group before EITHER commits and the SHORT-horizon
    # add commits LAST. Last-writer-wins on the group/count TTL would let that short
    # add SHRINK the shared count_key below the long park's horizon, so the count
    # expires while the long park is still pending: the reaper's ``record_answer``
    # claim then raises "pending count missing" every pass and the continuation never
    # fires. The set-or-extend-to-greater (``EXPIRE NX`` + ``EXPIRE GT``) refresh
    # converges the shared keys to the MAX horizon regardless of commit order.
    barrier = _EvalBarrierRedis()
    r = cast(Redis, barrier)  # the store methods type their client as ``Redis``
    settings = InteractionsSettings()

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield barrier

    monkeypatch.setattr(reaper_module, "client_ctx", _ctx)
    monkeypatch.setattr(reaper_module, "interactions_settings", lambda: settings)
    # The detached fire clears its durable due-record through the continuation seam.
    monkeypatch.setattr(continuation_module, "client_ctx", _ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    store = InteractionStore(settings.key_prefix)

    idle_ttl = 100
    margin = 10
    base = datetime.now(UTC)
    long_expiry = base + timedelta(seconds=300)  # horizon 300 + margin 10 = 310
    short_expiry = base + timedelta(seconds=150)  # horizon 150 + margin 10 = 160
    long_req = _async_req(store, iid="long", gid="raceg", expiry_at=long_expiry)
    short_req = _async_req(store, iid="short", gid="raceg", expiry_at=short_expiry)

    # SHORT reaches the eval barrier first (and so commits LAST); LONG arrives second,
    # releases the barrier, and commits FIRST — the order that would shrink the shared
    # TTLs under last-writer-wins.
    short_task = asyncio.create_task(
        store.add(r, short_req, idle_ttl=idle_ttl, continuation_fingerprint="fp-s", expiry_ttl_margin_seconds=margin)
    )
    await asyncio.sleep(0)
    assert barrier.arrived == 1  # short is parked at the barrier, pre-write
    long_task = asyncio.create_task(
        store.add(r, long_req, idle_ttl=idle_ttl, continuation_fingerprint="fp-l", expiry_ttl_margin_seconds=margin)
    )
    await asyncio.gather(short_task, long_task)

    group_key = store.group_key("raceg")
    count_key = store.count_key("raceg")
    long_state = store.state_key("long")
    short_state = store.state_key("short")

    # The shared group stream + count_key survive to the LONG horizon: the short add
    # could not shrink them. The long park's own state matches; the short park's own
    # state keeps its own (shorter) horizon.
    assert barrier._ttls[long_state] == 310
    assert barrier._ttls[short_state] == 160
    assert barrier._ttls[count_key] == barrier._ttls[long_state]
    assert barrier._ttls[group_key] == barrier._ttls[long_state]

    # Advance the store clock past the SHORT horizon but before the LONG one: the
    # short park's state has idle-expired, but the long park's state AND the shared
    # count survive — the long park is still answerable.
    barrier.advance(200)
    long_alive = await store.get_state(r, "long")
    assert long_alive is not None
    assert long_alive.status == "pending"
    assert await store.get_state(r, "short") is None

    # The reaper runs once wall-time passes both expiries. It reconciles the short
    # park's stale expiry member (state gone, no fire) and claims the LONG park through
    # ``record_answer`` — which finds a LIVE count and fires the continuation EXACTLY
    # once. Under last-writer-wins the count would be gone here and the claim would
    # raise "pending count missing".
    class _Clock:
        @staticmethod
        def now(tz=None):
            return long_expiry + timedelta(seconds=1)

    monkeypatch.setattr(reaper_module, "datetime", _Clock)
    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1
    await _drain()
    assert len(captured) == 1
    assert captured[0]["interaction_id"] == "long"
    assert captured[0]["fingerprint"] == "fp-l"
    assert captured[0]["answer"] == EXPIRY_ANSWER

    # No strand and no double-fire: a second pass claims nothing.
    assert await reaper_module.reap_expired_parks_once() == 0
    await _drain()
    assert len(captured) == 1


async def test_record_answer_enqueues_due_record_atomically_with_claim(wired):
    # The transactional-outbox invariant: the durable due-record is written in the SAME
    # MULTI as the claim, so the instant record_answer returns True — BEFORE any fire —
    # the answered state AND the due-record both exist. A crash right after the claim
    # can never leave a claimed answer with no due-record (the pre-existing (d) defect).
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="atom1"), idle_ttl=86400, continuation_fingerprint="fp-1"
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
        wired.fake, _async_req(wired.store, iid="nt1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    response = InteractionResponse(interaction_id="nt1", answer="go", answered_by="u", answered_at=datetime.now(UTC))
    with pytest.raises(RuntimeError, match="continuation-due timing"):
        await wired.store.record_answer(wired.fake, response, "ag", 60)


async def test_due_record_is_flow_blind_and_cleared_on_return(wired, monkeypatch):
    # The durable continuation-due record carries ONLY a registered tool NAME, the
    # generic {answer}, the stored identity, the key fingerprint, and an attempt count
    # — nothing flow/session/engine specific. A leak here is a platform layering
    # violation. The fire clears the record once ``run_tool`` returns.
    release = asyncio.Event()

    async def _block(identity, fingerprint, tool, interaction_id, answer):
        await release.wait()

    monkeypatch.setattr(continuation_module, "_run_continuation", _block)
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="fb1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    assert await ops.answer_interaction("fb1", "yes") == {"interaction_id": "fb1", "status": "answered"}
    await _drain()

    # While the fire is in flight the record stands and is flow-blind.
    raw = await wired.fake.hgetall(wired.store.continuation_due_key("fb1"))
    assert set(raw.keys()) == {"tool", "identity", "fingerprint", "answer", "attempts"}
    assert raw["tool"] == "resume_tool"
    assert raw["identity"] == "svc-key"
    assert raw["fingerprint"] == "fp-1"
    assert json.loads(raw["answer"]) == "yes"
    assert raw["attempts"] == "0"

    # ``run_tool`` returns → the record is cleared, index member dropped.
    release.set()
    await _drain()
    assert await wired.fake.hgetall(wired.store.continuation_due_key("fb1")) == {}
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == []


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

    async def _flaky(identity, fingerprint, tool, interaction_id, answer):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("worker crashed mid-resume")
        applied.append({"identity": identity, "tool": tool, "answer": answer})

    monkeypatch.setattr(continuation_module, "_run_continuation", _flaky)
    await wired.store.add(
        wired.fake, _async_req(wired.store, iid="d1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    caplog.set_level(logging.ERROR)
    assert await ops.answer_interaction("d1", "go") == {"interaction_id": "d1", "status": "answered"}
    await _drain()
    # The first fire crashed: nothing applied, the record was NOT cleared.
    assert applied == []
    assert attempts["n"] == 1
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == ["d1"]
    assert any("continuation" in rec.message and "failed" in rec.message for rec in caplog.records)

    # Past the first-attempt window, the reaper redelivers — this fire returns.
    await asyncio.sleep(0.03)
    assert await reaper_module.redeliver_due_continuations_once() == 1
    await _drain()
    assert len(applied) == 1
    assert applied[0] == {"identity": "svc-key", "tool": "resume_tool", "answer": "go"}

    # The record is cleared; a further pass redelivers nothing (exactly once applied).
    assert await reaper_module.redeliver_due_continuations_once() == 0
    await _drain()
    assert len(applied) == 1
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == []


async def test_redelivery_claim_is_idempotent_within_a_backoff_window(wired):
    # Two racing redelivery claims on the SAME due record within one backoff window
    # yield exactly one re-fire: the first advances the record's next-attempt score and
    # returns it, the second sees it no longer due and declines. At-least-once never
    # becomes a storm; consumer idempotency covers the rare cross-window double-fire.
    now = datetime.now(UTC)
    past_ms = int(now.timestamp() * 1000) - 1000
    await _seed_due(wired.store, wired.fake, "i1", answer={"a": 1}, first_attempt_at_ms=past_ms)
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


def test_execution_identity_bridge_reflects_the_bound_skeleton_identity():
    # Importing the skeleton continuation module wires skeleton's execution identity into the
    # contract park bridge: the accessor reads the CURRENTLY bound identity as (key, fingerprint)
    # — what a park records — and the binder is registered for the out-of-band abandonment fire.
    from tai42_contract.interactions import continuation as contract_cont
    from tai42_contract.interactions import current_execution_identity

    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity

    assert contract_cont._execution_identity_accessor is not None
    assert contract_cont._execution_identity_binder is not None

    # No identity bound: the accessor yields none, so a park under no identity records none.
    assert current_execution_identity() == (None, "")

    token = set_execution_identity(CallerIdentity(user_id="user-x", execution_key_fingerprint="fp-x"))
    try:
        # The same two values the durable continuation record captures (user_id + fingerprint).
        assert current_execution_identity() == ("user-x", "fp-x")
    finally:
        reset_execution_identity(token)
    assert current_execution_identity() == (None, "")


async def test_redelivery_pass_survives_a_poison_member(wired, monkeypatch, caplog):
    # One member whose claim deterministically raises must not abort the whole pass and
    # starve the healthy members: the poison member is logged loudly and skipped, and a
    # co-due healthy record still redelivers in the same pass.
    now = datetime.now(UTC)
    past_ms = int(now.timestamp() * 1000) - 1000
    await _seed_due(wired.store, wired.fake, "poison", answer={"p": 1}, first_attempt_at_ms=past_ms)
    await _seed_due(wired.store, wired.fake, "healthy", answer={"h": 1}, first_attempt_at_ms=past_ms)

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


async def test_expiry_pass_survives_a_poison_member(wired, monkeypatch, caplog):
    # One expiry-index member whose per-member work deterministically raises must not
    # abort the whole pass and starve the rest: it is logged loudly and left for the
    # next pass, while a co-due member is still reconciled off in the same pass.
    now = datetime.now(UTC)
    past_ms = int(now.timestamp() * 1000) - 1000
    await wired.fake.zadd(wired.store.pending_expiry_key, {"poison": past_ms, "gone": past_ms})

    real_get_state = wired.store.get_state

    async def _get_state(r, interaction_id):
        if interaction_id == "poison":
            raise RuntimeError("poison park")
        # A member whose state has vanished is reconciled off the expiry index.
        return await real_get_state(r, interaction_id)

    monkeypatch.setattr(wired.store, "get_state", _get_state)
    monkeypatch.setattr(reaper_module, "InteractionStore", lambda _prefix: wired.store)

    caplog.set_level(logging.ERROR)
    assert await reaper_module.reap_expired_parks_once() == 0
    assert any("poison" in rec.message and "skipping it this pass" in rec.message for rec in caplog.records)
    # The healthy (vanished) member was reconciled off despite the poison member; the
    # poison member is left for the next pass rather than starving the index.
    remaining = await wired.fake.zrangebyscore(wired.store.pending_expiry_key, 0, past_ms + 1)
    assert remaining == ["poison"]


async def test_reaper_loop_survives_a_raising_pass(monkeypatch, caplog):
    # A pass that raises is ERROR-logged and the loop CONTINUES to the next interval —
    # a silently dead reaper would strand every async park past its expiry.
    settings = InteractionsSettings(expiry_reaper_interval_seconds=0.01)
    monkeypatch.setattr(reaper_module, "interactions_settings", lambda: settings)

    calls = {"n": 0}

    async def _pass() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(reaper_module, "reap_expired_parks_once", _pass)

    async def _no_redeliver() -> int:
        return 0

    monkeypatch.setattr(reaper_module, "redeliver_due_continuations_once", _no_redeliver)

    caplog.set_level(logging.ERROR)
    task = asyncio.create_task(reaper_module.run_expiry_reaper_loop())
    while calls["n"] < 2:  # the failing pass AND at least one that follows it
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2
    assert any("expiry reaper pass failed" in r.message for r in caplog.records)
