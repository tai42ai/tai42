"""The async park's continuation dispatch: both answer doors and the expiry reaper
funnel through the single post-claim seam, so a resolved park fires its stored
continuation EXACTLY ONCE, bound to the STORED identity (never the answerer's),
carrying ``{interaction_id, answer}``. The reaper resolves an expired park by the
generic EXPIRY answer and prunes it, run-and-clear clears the record on return, and
the detached fire runs out of band.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from redis.asyncio import Redis
from tai42_contract.access_control import reset_request_user_id, set_request_user_id
from tai42_contract.interactions import AnswerFormat, InteractionRequest, SuspendedInteraction

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.continuation import EXPIRY_ANSWER
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.operations import interactions as ops

from ._continuation_support import (
    EvalBarrierRedis,
    RecordingHooks,
    async_req,
    configure_interactions_store,
    drain,
    make_captured,
    make_wired,
)


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


@pytest.fixture
def captured(monkeypatch):
    return make_captured(monkeypatch)


async def test_answer_door_fires_continuation_under_stored_identity(wired, captured):
    await wired.store.add(wired.fake, async_req(wired.store, iid="a1"), idle_ttl=86400, continuation_fingerprint="fp-1")
    # Answerer is a DIFFERENT identity than the stored continuation identity.
    token = set_request_user_id("answerer-x")
    try:
        assert await ops.answer_interaction("a1", "yes") == {"interaction_id": "a1", "status": "answered"}
    finally:
        reset_request_user_id(token)
    await drain()
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
    await drain()
    assert captured == []


async def test_reaper_fires_expiry_continuation_once_then_prunes(wired, captured):
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="e1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1
    await drain()
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
    await drain()
    assert len(captured) == 1


async def test_answer_racing_expiry_yields_exactly_one_continuation(wired, captured):
    past = datetime.now(UTC) - timedelta(seconds=1)
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="r1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    # A human answer lands first and claims the park.
    assert await ops.answer_interaction("r1", "human") == {"interaction_id": "r1", "status": "answered"}
    await drain()
    assert len(captured) == 1
    assert captured[0]["answer"] == "human"
    # The reaper then finds it already answered: it claims nothing and fires nothing.
    assert await reaper_module.reap_expired_parks_once() == 0
    await drain()
    assert len(captured) == 1


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
        on_expiry="resume",
        channel="telegram",
        recipient="@ops",
    )
    await wired.store.add(wired.fake, req, idle_ttl=86400, continuation_fingerprint="fp-1")

    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1
    await drain()

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
        wired.fake, async_req(wired.store, iid="n1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )

    assert await reaper_module.reap_expired_parks_once() == 1
    await drain()

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
        wired.fake, async_req(wired.store, iid="h1", expiry_at=past), idle_ttl=86400, continuation_fingerprint="fp-1"
    )

    fired = await reaper_module.reap_expired_parks_once()
    assert fired == 1  # the pass did not abort on the hooks failure
    await drain()
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
    await drain()
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
        async_req(wired.store, iid="p1", expiry_at=expiry),
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
    await drain()
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

    async def _fake_run_tool(tool, arguments, *, continues_chain=None):
        identity = get_execution_identity()
        seen.append({"tool": tool, "arguments": arguments, "user_id": identity.user_id if identity else None})
        return {"ran": True}

    # ``run_tool`` is the platform tool-registry seam the real continuation dispatches
    # through; fake only it, so the REAL ``bind_execution_identity`` + detached-task
    # path still run.
    fake_app = SimpleNamespace(tools=SimpleNamespace(run_tool=_fake_run_tool))
    monkeypatch.setattr(continuation_module, "tai42_app", fake_app)

    await wired.store.add(
        wired.fake, async_req(wired.store, iid="rc1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    token = set_request_user_id("answerer-x")  # a DIFFERENT identity resolves the park
    try:
        assert await ops.answer_interaction("rc1", "go") == {"interaction_id": "rc1", "status": "answered"}
    finally:
        reset_request_user_id(token)
    await drain()
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

    async def _fake_run_tool(tool, arguments, *, continues_chain=None):
        seen.append(get_resume_origin())
        return {"ran": True}

    fake_app = SimpleNamespace(tools=SimpleNamespace(run_tool=_fake_run_tool))
    monkeypatch.setattr(continuation_module, "tai42_app", fake_app)

    await wired.store.add(
        wired.fake, async_req(wired.store, iid="ro1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    token = set_request_user_id("answerer-x")
    try:
        assert await ops.answer_interaction("ro1", "go") == {"interaction_id": "ro1", "status": "answered"}
    finally:
        reset_request_user_id(token)
    await drain()
    assert seen == ["ro1"]  # the run_tool re-entry saw the parked interaction's id
    assert get_resume_origin() is None  # nothing leaks outside the detached fire


async def test_dispatch_continuation_retains_task_until_done(wired, monkeypatch):
    # The detached continuation task is held by a strong reference while in flight, so
    # it can never be GC-collected mid-resume; the reference is dropped when it ends.
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(
        identity, fingerprint, tool, interaction_id, answer, park_context=None, park_asked_by=(), *, mark_detached=True
    ):
        started.set()
        await release.wait()
        return SuspendedInteraction(interaction_id=interaction_id)

    monkeypatch.setattr(continuation_module, "_run_continuation", _slow)

    continuation_module.dispatch_continuation(wired.store, async_req(wired.store, iid="gc1"), "fp-1", "answer")
    await started.wait()
    assert any(t.get_name() == "interaction-continuation-gc1" for t in continuation_module._CONTINUATION_TASKS)
    release.set()
    await drain()
    assert not any(t.get_name() == "interaction-continuation-gc1" for t in continuation_module._CONTINUATION_TASKS)


async def test_concurrent_same_group_parks_never_shrink_shared_ttls(monkeypatch, captured):
    # Two async parks into the SAME group with divergent expiries (long + short),
    # interleaved so BOTH read the group before EITHER commits and the SHORT-horizon
    # add commits LAST. Last-writer-wins on the group/count TTL would let that short
    # add SHRINK the shared count_key below the long park's horizon, so the count
    # expires while the long park is still pending: the reaper's ``record_answer``
    # claim then raises "pending count missing" every pass and the continuation never
    # fires. The set-or-extend-to-greater (``EXPIRE NX`` + ``EXPIRE GT``) refresh
    # converges the shared keys to the MAX horizon regardless of commit order.
    barrier = EvalBarrierRedis()
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
    long_req = async_req(store, iid="long", gid="raceg", expiry_at=long_expiry)
    short_req = async_req(store, iid="short", gid="raceg", expiry_at=short_expiry)

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
    await drain()
    assert len(captured) == 1
    assert captured[0]["interaction_id"] == "long"
    assert captured[0]["fingerprint"] == "fp-l"
    assert captured[0]["answer"] == EXPIRY_ANSWER

    # No strand and no double-fire: a second pass claims nothing.
    assert await reaper_module.reap_expired_parks_once() == 0
    await drain()
    assert len(captured) == 1


async def test_due_record_is_flow_blind_and_cleared_on_return(wired, monkeypatch):
    # The durable continuation-due record carries ONLY a registered tool NAME, the
    # generic {answer}, the stored identity, the key fingerprint, and an attempt count
    # — nothing flow/session/engine specific. A leak here is a platform layering
    # violation. The fire clears the record once ``run_tool`` returns.
    release = asyncio.Event()

    async def _block(
        identity, fingerprint, tool, interaction_id, answer, park_context=None, park_asked_by=(), *, mark_detached=True
    ):
        await release.wait()
        return SuspendedInteraction(interaction_id=interaction_id)

    monkeypatch.setattr(continuation_module, "_run_continuation", _block)
    await wired.store.add(
        wired.fake, async_req(wired.store, iid="fb1"), idle_ttl=86400, continuation_fingerprint="fp-1"
    )
    assert await ops.answer_interaction("fb1", "yes") == {"interaction_id": "fb1", "status": "answered"}
    await drain()

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
    await drain()
    assert await wired.fake.hgetall(wired.store.continuation_due_key("fb1")) == {}
    assert await wired.store.due_continuations(wired.fake, datetime.now(UTC) + timedelta(hours=1)) == []


def test_execution_identity_bridge_reflects_the_bound_skeleton_identity():
    # Importing the skeleton continuation module wires skeleton's execution identity into the
    # contract park bridge: the accessor reads the CURRENTLY bound identity as (key, fingerprint)
    # — what a park records — and the binder is registered for an out-of-band fire.
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


async def test_continuation_resume_runs_detached(monkeypatch):
    # A continuation resume runs OUT OF BAND on a fresh task with no live caller, so it
    # must be flagged detached — else a set ``TAI_TURN_TIMEOUT_SECONDS`` would arm the
    # turn budget around the resume and cancel a legitimately long resume mid-flight.
    from tai42_contract.app import tai42_app
    from tai42_kit.utils.detached_util import in_detached_run

    seen: dict[str, bool] = {}

    @asynccontextmanager
    async def _fake_bind(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)

    class _Tools:
        async def run_tool(self, key, arguments, *, offload_sync=False, continues_chain=None):
            seen["detached"] = in_detached_run()
            return None

    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=_Tools()))

    await continuation_module._run_continuation("id-1", None, "resume_tool", "iid-1", {"a": 1})

    assert seen["detached"] is True
    # The flag is unwound once the resume ends — never leaked past it.
    assert in_detached_run() is False
