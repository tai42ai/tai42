"""The whole-chain kill and expiry-kill: the one teardown seam every withdrawal door routes through.

A kill prunes the park, clears its continuation-due record, fires each driver's teardown with the
run-authorization context bound, and delivers the run's single FAILED once through the platform
ladder — durably, so a crash between teardown and delivery redelivers. The four conversation
teardown doors (thread/person/route delete, person merge), the cancel door and the expiry reaper's
``on_expiry="kill"`` branch all reach it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    AnswerFormat,
    InteractionRequest,
    ParkResumeUnauthorizedError,
    register_park_kill_handler,
)
from tai42_contract.interactions import continuation as contract_continuation
from tai42_contract.states import StateContext, SubjectCandidates

from tai42_skeleton.interactions import authorization
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import kill as kill_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.kill import kill_park

from ._continuation_support import RecordingHooks, configure_interactions_store, make_wired


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    w = make_wired(monkeypatch, fake_redis, fake_client_ctx)
    # The FAILED delivery fires the address by NAME through run_tool; point the resume-auth store
    # read at the same fake so a teardown handler's authorization check resolves against it.
    monkeypatch.setattr(authorization, "client_ctx", _fake_ctx(w.fake))
    monkeypatch.setattr(authorization, "interactions_settings", lambda: w.settings)
    monkeypatch.setattr(authorization, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(authorization, "InteractionStore", lambda _prefix: w.store)
    return w


@pytest.fixture
def kill_handlers(monkeypatch):
    # A fresh, isolated handler list per test (the seam is a process-global registry).
    handlers: list[Any] = []
    monkeypatch.setattr(contract_continuation, "_park_kill_handlers", handlers)
    return handlers


def _fake_ctx(fake):
    @asynccontextmanager
    async def _ctx(_cls, _settings=None):
        yield fake

    return _ctx


class _Tools:
    """A ``run_tool`` fake that dispatches by NAME and records the ambient delivery-fire context."""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, key, arguments, *, offload_sync=False, continues_chain=None):
        from tai42_skeleton.runs.chokepoint import get_delivery_fire

        self.calls.append({"key": key, "arguments": dict(arguments), "delivery_fire": get_delivery_fire()})
        handler = self.handlers.get(key)
        return handler(arguments) if handler is not None else None


def _wire_tools(monkeypatch, handlers: dict[str, Any]) -> _Tools:
    tools = _Tools(handlers)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools))
    return tools


def _cid(run_delivery_id: str) -> str:
    return continuation_module._completion_id(run_delivery_id)


async def _add_park(
    wired,
    *,
    iid: str,
    gid: str = "kg",
    delivery: tuple[str, dict[str, Any]] | None,
    run_delivery_id: str,
    candidates: SubjectCandidates | None = None,
    to: str = "user",
    thread_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    request = InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key(iid),
        created_at=now,
        timeout_at=deadline,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=StateContext(door="conversation", candidates=candidates)
        if candidates is not None
        else None,
        expiry_at=deadline,
        run_delivery_id=run_delivery_id,
        delivery=delivery,
    )
    await wired.store.add(
        wired.fake,
        request,
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        thread_id=thread_id,
        to=to,  # type: ignore[arg-type]
        delivery={"tool": delivery[0], "context": delivery[1]} if delivery is not None else None,
        run_delivery_id=run_delivery_id,
    )


def _recording_handler(record: list[tuple[str, str]]):
    async def _handler(interaction_id: str, reason: str) -> None:
        record.append((interaction_id, reason))

    return _handler


async def _kill_due_present(wired, iid: str) -> bool:
    return bool(await wired.fake.hgetall(wired.store.kill_due_key(iid)))


async def _answered_park_with_expired_state(
    wired, *, iid: str, run_delivery_id: str, candidates: SubjectCandidates | None = None
) -> None:
    """An answered async park whose state hash has TTL-expired while its continuation-due record stands."""
    from tai42_contract.interactions import InteractionResponse

    now = datetime.now(UTC)
    await _add_park(
        wired,
        iid=iid,
        delivery=("deliver_tool", {"thread_id": "t1"}),
        run_delivery_id=run_delivery_id,
        candidates=candidates,
    )
    await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id=iid, answer="y", answered_by="op", answered_at=now),
        "kg",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )
    # The state hash TTL-expires; the continuation-due record (and the subject index) still stand.
    await wired.fake.delete(wired.store.state_key(iid))
    assert bool(await wired.fake.hgetall(wired.store.continuation_due_key(iid)))


# --- the core kill: prune + handler + single FAILED --------------------------------------


async def test_kill_prunes_fires_the_handler_and_delivers_the_door_failed(wired, monkeypatch, kill_handlers):
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    await _add_park(wired, iid="k1", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-1")

    result = await kill_park(wired.fake, wired.store, "k1", "kg", reason="cancelled")

    assert result == "pruned"
    # The driver teardown fired with the killed interaction + reason.
    assert record == [("k1", "cancelled")]
    # The run's single FAILED fired the door ONCE, inside the delivery-fire context, keyed by the run.
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 1
    assert fires[0]["arguments"]["status"] == PARK_COMPLETION_FAILED
    assert fires[0]["arguments"]["completion_id"] == _cid("rd-1")
    assert fires[0]["delivery_fire"] == _cid("rd-1")
    # State pruned, continuation-due cleared, kill-due cleared (teardown + delivery both committed).
    assert await wired.store.get_state(wired.fake, "k1") is None
    assert not await _kill_due_present(wired, "k1")


async def test_kill_entering_a_nested_caller_ask_fires_the_runs_own_door_once(wired, monkeypatch, kill_handlers):
    # A nested ``to="caller"`` ask stores the SAME run address + id as the route's ask, so a kill
    # entering at it reads the run's real address (not None) and fires the run's door FAILED once.
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    register_park_kill_handler(_recording_handler([]))
    await _add_park(
        wired, iid="nested", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-9", to="caller"
    )
    await kill_park(wired.fake, wired.store, "nested", "kg", reason="cancelled")
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 1
    assert fires[0]["arguments"]["completion_id"] == _cid("rd-9")


async def test_receiver_less_kill_subject_tracks_a_failed_outcome(wired, monkeypatch, kill_handlers):
    _wire_tools(monkeypatch, {})
    register_park_kill_handler(_recording_handler([]))
    candidates = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pX"})
    await _add_park(wired, iid="rl", delivery=None, run_delivery_id="rd-2", candidates=candidates)

    await kill_park(wired.fake, wired.store, "rl", "kg", reason="cancelled")

    # No address, but a subject: a ``failed`` waiting outcome keyed by the run's completion id.
    outcome = await wired.store.claim_outcome(wired.fake, _cid("rd-2"))
    assert outcome is not None
    assert outcome.status == "failed"


async def test_kill_clears_a_buffered_siblings_due_record_even_when_answered(wired, monkeypatch, kill_handlers):
    # A buffered/answered sibling (a parallel branch) has a live continuation-due record; the
    # kill MULTI clears it UNCONDITIONALLY so nothing redelivers into the torn-down run.
    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    register_park_kill_handler(_recording_handler([]))
    await _add_park(wired, iid="sib", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-3")
    # Drive it to answered, enqueuing its continuation-due record.
    from tai42_contract.interactions import InteractionResponse

    now = datetime.now(UTC)
    await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="sib", answer="y", answered_by="op", answered_at=now),
        "kg",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )
    assert bool(await wired.fake.hgetall(wired.store.continuation_due_key("sib")))

    result = await kill_park(wired.fake, wired.store, "sib", "kg", reason="erased")

    assert result == "answered"
    # The sibling's continuation-due record + index member are gone; the reaper redelivers nothing.
    assert not bool(await wired.fake.hgetall(wired.store.continuation_due_key("sib")))
    assert await wired.store.due_continuations(wired.fake, now + timedelta(days=1)) == []


async def test_kill_of_an_answered_park_whose_state_aged_out_tears_down_now(wired, monkeypatch, kill_handlers):
    # A running entry whose state hash TTL-expired while its continuation-due record still stands: the
    # kill MULTI clears the entry, so the caller tears it down NOW instead of deferring to the reaper.
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    await _answered_park_with_expired_state(wired, iid="aged", run_delivery_id="rd-aged")

    result = await kill_park(wired.fake, wired.store, "aged", "kg", reason="cancelled")

    assert result == "pruned"
    # The teardown fired once and the run's single FAILED fired once, keyed by the run.
    assert record == [("aged", "cancelled")]
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 1
    assert fires[0]["arguments"]["status"] == PARK_COMPLETION_FAILED
    assert fires[0]["arguments"]["completion_id"] == _cid("rd-aged")
    # The kill-due + continuation-due records are cleared, so the reaper redelivers nothing.
    assert not await _kill_due_present(wired, "aged")
    assert not bool(await wired.fake.hgetall(wired.store.continuation_due_key("aged")))


async def test_subject_erase_of_an_answered_park_whose_state_aged_out_counts_it_reached(
    wired, monkeypatch, kill_handlers
):
    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    candidates = SubjectCandidates(target_kind="agent", target_name="scope-k", by_kind={"person": "pK"})
    await _answered_park_with_expired_state(wired, iid="agedk", run_delivery_id="rd-agedk", candidates=candidates)

    reached = await kill_module.kill_parks_for_subject("person", "pK", reason="person_erased")

    # The aged-out running entry is torn down and REPORTED reached, not silently omitted as "gone".
    assert reached == ["agedk"]
    assert record == [("agedk", "person_erased")]
    assert not await _kill_due_present(wired, "agedk")


# --- the run-authorization context bound around the teardown handler ---------------------


async def test_kill_binds_run_authorization_so_the_handler_authorizes_on_the_run_identity(
    wired, monkeypatch, kill_handlers
):
    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    # An ANCESTOR interaction of the SAME run (shares run_delivery_id ``rd-4``).
    await _add_park(wired, iid="ancestor", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-4")
    await _add_park(wired, iid="leaf", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-4")
    authorized: list[bool] = []

    async def _handler(interaction_id: str, reason: str) -> None:
        # A cross-driver teardown notify: authorize on the shared run identity, not the origin.
        await authorization.assert_resume_authorized("ancestor")
        authorized.append(True)

    register_park_kill_handler(_handler)
    await kill_park(wired.fake, wired.store, "leaf", "kg", reason="cancelled")
    assert authorized == [True]
    # Outside a kill's bound context the same assert is refused (no resume drive on the stack).
    with pytest.raises(ParkResumeUnauthorizedError):
        await authorization.assert_resume_authorized("ancestor")


# --- durability: a transient teardown raise keeps the kill-due for the reaper -------------


async def test_transient_handler_raise_propagates_keeps_kill_due_and_the_reaper_redelivers(
    wired, monkeypatch, kill_handlers
):
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    fail_first = {"count": 0}

    async def _handler(interaction_id: str, reason: str) -> None:
        fail_first["count"] += 1
        if fail_first["count"] == 1:
            raise RuntimeError("ancestor not torn down yet")

    register_park_kill_handler(_handler)
    await _add_park(wired, iid="tr", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-5")

    # The first kill propagates the transient raise; the kill-due record is KEPT, the FAILED not fired.
    with pytest.raises(RuntimeError):
        await kill_park(wired.fake, wired.store, "tr", "kg", reason="cancelled")
    assert await _kill_due_present(wired, "tr")
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)

    # Advance the backoff window (the record is seeded one reaper interval out): now it is due.
    await wired.fake.zadd(wired.store.kill_due_index_key, {"tr": 0})
    # The reaper's kill-due leg redelivers: the handler now returns, the FAILED fires, the record clears.
    redelivered = await reaper_module.redeliver_due_kills_once()
    assert redelivered == 1
    assert any(c["key"] == "deliver_tool" for c in tools.calls)
    assert not await _kill_due_present(wired, "tr")


async def test_kill_due_redelivered_after_a_simulated_crash(wired, monkeypatch, kill_handlers):
    # A crash AFTER the teardown MULTI but BEFORE the FAILED delivery: the kill-due record stands
    # (seeded here as the MULTI would leave it), and the reaper redelivers the kill exactly once.
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    # Seed a durable kill-due record + due index member (the shape ``enqueue_kill`` commits).
    await wired.fake.hset(
        wired.store.kill_due_key("crash"),
        mapping={
            "reason": "cancelled",
            "attempts": "0",
            "deadline_ms": str(int(datetime.now(UTC).timestamp() * 1000) + 3_600_000),
            "delivery": '{"tool": "deliver_tool", "context": {"thread_id": "t1"}}',
            "run_delivery_id": "rd-6",
        },
    )
    await wired.fake.zadd(wired.store.kill_due_index_key, {"crash": 0})

    redelivered = await reaper_module.redeliver_due_kills_once()

    assert redelivered == 1
    assert record == [("crash", "cancelled")]
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 1
    assert fires[0]["arguments"]["completion_id"] == _cid("rd-6")
    assert not await _kill_due_present(wired, "crash")


async def test_kill_due_abandoned_past_the_deadline_emits_once_and_commits_no_failed(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    register_park_kill_handler(_recording_handler([]))
    # A kill-due record already PAST its give-up deadline (the teardown never completed).
    await wired.fake.hset(
        wired.store.kill_due_key("dead"),
        mapping={
            "reason": "cancelled",
            "attempts": "3",
            "deadline_ms": str(int(datetime.now(UTC).timestamp() * 1000) - 1000),
            "run_delivery_id": "rd-7",
        },
    )
    await wired.fake.zadd(wired.store.kill_due_index_key, {"dead": 0})

    assert await reaper_module.redeliver_due_kills_once() == 0

    # Emitted ONCE, carrying the run identity; NO door FAILED committed; the record is cleared.
    assert len(hooks.events) == 1
    assert hooks.events[0].topic == reaper_module.KILL_ABANDONED_EVENT_TOPIC == "interactions_kill_abandoned"
    assert hooks.events[0].payload == {"interaction_id": "dead", "run_delivery_id": "rd-7"}
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    assert not await _kill_due_present(wired, "dead")
    # No later pass redelivers it.
    assert await reaper_module.redeliver_due_kills_once() == 0
    assert len(hooks.events) == 1


# --- expiry: kill vs resume --------------------------------------------------------------


async def test_expiry_kill_tears_the_run_down_and_fires_the_door_failed(wired, monkeypatch, kill_handlers):
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    past = datetime.now(UTC) - timedelta(seconds=1)
    request = InteractionRequest(
        interaction_id="ek",
        group_id="eg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("ek"),
        created_at=datetime.now(UTC),
        timeout_at=past,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=past,
        on_expiry="kill",
        run_delivery_id="rd-8",
        delivery=("deliver_tool", {"thread_id": "t1"}),
    )
    await wired.store.add(
        wired.fake,
        request,
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        delivery={"tool": "deliver_tool", "context": {"thread_id": "t1"}},
        run_delivery_id="rd-8",
    )

    assert await reaper_module.reap_expired_parks_once() == 1
    # The whole chain was torn down and the run's door FAILED delivered.
    assert record == [("ek", "expired")]
    assert any(c["key"] == "deliver_tool" and c["arguments"]["status"] == PARK_COMPLETION_FAILED for c in tools.calls)
    assert await wired.store.get_state(wired.fake, "ek") is None


async def test_expiry_kill_skips_a_park_answered_in_the_claim_window(wired, monkeypatch, kill_handlers):
    # An answer that commits between the reaper's read and its atomic claim: the single-park
    # pending-gate (``KILL_ACT_ON_PENDING``) sees the park answered and does NOTHING — no teardown,
    # no FAILED, no due-record deletion — so the answer's own continuation owns the run.
    from tai42_contract.interactions import InteractionResponse

    from tai42_skeleton.interactions.store import KILL_ACT_ON_PENDING

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    await _add_park(wired, iid="race", delivery=("deliver_tool", {"thread_id": "t1"}), run_delivery_id="rd-race")
    # The answer lands (the run is now resuming, its continuation-due record enqueued).
    now = datetime.now(UTC)
    await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="race", answer="y", answered_by="op", answered_at=now),
        "kg",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )

    # The expiry door's kill, on the single-park precondition, claims only a pending park.
    result = await kill_park(wired.fake, wired.store, "race", "kg", reason="expired", act_on=KILL_ACT_ON_PENDING)

    assert result == "answered"
    # No teardown fired, no FAILED delivered, and the answer's continuation-due record is intact.
    assert record == []
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    assert bool(await wired.fake.hgetall(wired.store.continuation_due_key("race")))
    assert not await _kill_due_present(wired, "race")


async def test_expiry_resume_takes_the_continuation_path_not_the_kill(wired, monkeypatch, kill_handlers):
    # ``on_expiry="resume"`` fires the stored continuation (the resume path), never the kill.
    killed: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(killed))
    captured: list[str] = []

    async def _stub(
        identity, fingerprint, tool, interaction_id, answer, park_context=None, park_asked_by=(), *, mark_detached=True
    ):
        captured.append(interaction_id)
        from tai42_contract.interactions import SuspendedInteraction

        return SuspendedInteraction(interaction_id=interaction_id)

    monkeypatch.setattr(continuation_module, "_run_continuation", _stub)
    past = datetime.now(UTC) - timedelta(seconds=1)
    request = InteractionRequest(
        interaction_id="er",
        group_id="eg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("er"),
        created_at=datetime.now(UTC),
        timeout_at=past,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=past,
        on_expiry="resume",
        run_delivery_id="rd-r",
    )
    await wired.store.add(wired.fake, request, idle_ttl=86400, continuation_fingerprint="fp-1", run_delivery_id="rd-r")

    assert await reaper_module.reap_expired_parks_once() == 1
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured == ["er"]  # the continuation fired
    assert killed == []  # nothing was killed


# --- the waiting-outcome retention sweep -------------------------------------------------


async def test_retention_sweep_drops_an_aged_untaken_outcome_once(wired, monkeypatch, kill_handlers, caplog):
    import logging

    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    caplog.set_level(logging.ERROR)
    cands = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pS"})
    await wired.store.add_outcome(
        wired.fake,
        completion_id="cid-s",
        interaction_id="iid-s",
        status="finished",
        result={"v": 1},
        candidates=cands,
        retention_ttl=2 * wired.settings.idle_ttl_seconds,
        run_delivery_id="rd-s",
    )
    # Age its retention-index member past the horizon so the sweep sees it due.
    await wired.fake.zadd(wired.store.outcome_retention_index_key, {"cid-s": 0})

    assert await reaper_module.sweep_untaken_outcomes_once() == 1

    # Emitted ONCE naming the run, logged at error, and the outcome row is gone.
    assert len(hooks.events) == 1
    assert hooks.events[0].topic == reaper_module.OUTCOME_DROPPED_UNTAKEN_EVENT_TOPIC
    assert hooks.events[0].payload == {
        "completion_id": "cid-s",
        "interaction_id": "iid-s",
        "run_delivery_id": "rd-s",
        "subjects": {"target_kind": "agent", "target_name": "a", "by_kind": {"person": "pS"}},
    }
    assert any("dropped untaken" in rec.message for rec in caplog.records)
    assert await wired.store.claim_outcome(wired.fake, "cid-s") is None
    # A second pass finds nothing and emits nothing.
    assert await reaper_module.sweep_untaken_outcomes_once() == 0
    assert len(hooks.events) == 1


async def test_retention_sweep_never_emits_for_a_taken_outcome(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    cands = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pT"})
    await wired.store.add_outcome(
        wired.fake,
        completion_id="cid-t",
        interaction_id="iid-t",
        status="finished",
        result=1,
        candidates=cands,
        retention_ttl=3600,
        run_delivery_id="rd-t",
    )
    # The subject owner takes it, dropping its retention-index member in the SAME atomic step.
    assert await wired.store.claim_outcome(wired.fake, "cid-t") is not None
    # The sweep no longer lists it (the take removed the member), so it drops nothing and emits nothing.
    assert await reaper_module.sweep_untaken_outcomes_once() == 0
    assert hooks.events == []


async def test_retention_sweep_reconciles_an_index_member_whose_row_is_gone(wired, monkeypatch, kill_handlers, caplog):
    import logging

    from tai42_skeleton.hooks import cache as hooks_cache

    hooks = RecordingHooks()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    caplog.set_level(logging.ERROR)
    cands = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pO"})
    await wired.store.add_outcome(
        wired.fake,
        completion_id="cid-o",
        interaction_id="iid-o",
        status="finished",
        result={"v": 1},
        candidates=cands,
        retention_ttl=2 * wired.settings.idle_ttl_seconds,
        run_delivery_id="rd-o",
    )
    # The Redis TTL backstop deleted the row, but the no-TTL retention member lingers (aged past the
    # horizon): the exact orphan the sweep must reconcile rather than re-read every pass.
    await wired.fake.delete(wired.store.outcome_key("cid-o"))
    await wired.fake.zadd(wired.store.outcome_retention_index_key, {"cid-o": 0})

    assert await reaper_module.sweep_untaken_outcomes_once() == 1

    # The silent loss is still reported ONCE, logged at error, carrying only the completion id the
    # member holds (interaction/run ids unavailable — the row is gone — so null).
    assert len(hooks.events) == 1
    assert hooks.events[0].topic == reaper_module.OUTCOME_DROPPED_UNTAKEN_EVENT_TOPIC
    assert hooks.events[0].payload == {
        "completion_id": "cid-o",
        "interaction_id": None,
        "run_delivery_id": None,
        "subjects": None,
    }
    assert any("TTL backstop" in rec.message for rec in caplog.records)
    # The orphaned index members are gone from both zsets, so a second pass is a no-op.
    assert await wired.fake.zrangebyscore(wired.store.outcome_retention_index_key, 0, 2**63) == []
    assert await wired.fake.zrangebyscore(wired.store.open_caller_key, 0, 2**63) == []
    assert await reaper_module.sweep_untaken_outcomes_once() == 0
    assert len(hooks.events) == 1


# --- the erase / merge doors reach the kill seam -----------------------------------------


async def test_erase_by_subject_key_reaches_a_park_on_another_thread(wired, monkeypatch, kill_handlers):
    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    # A park addressed to person ``pE`` under a scope, reachable only through the subject index.
    candidates = SubjectCandidates(target_kind="agent", target_name="scope-x", by_kind={"person": "pE"})
    await _add_park(
        wired,
        iid="erased",
        delivery=("deliver_tool", {"thread_id": "t1"}),
        run_delivery_id="rd-e",
        candidates=candidates,
    )

    reached = await kill_module.kill_parks_for_subject("person", "pE", reason="person_erased")

    assert reached == ["erased"]
    assert record == [("erased", "person_erased")]
    assert await wired.store.get_state(wired.fake, "erased") is None


async def test_merge_rekeys_the_index_stored_context_and_thread_index(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.interactions.helper import rekey_parks_for_merge

    candidates = SubjectCandidates(
        target_kind="agent",
        target_name="scope-m",
        by_kind={"person": "absorbed", "thread": "bridge:@person:absorbed"},
    )
    await _add_park(wired, iid="m1", delivery=None, run_delivery_id="rd-m", candidates=candidates)

    await rekey_parks_for_merge("absorbed", "survivor")

    # The subject index moved from the absorbed person key to the survivor's.
    assert await wired.fake.smembers(wired.store.subject_parks_key("agent", "scope-m", "person", "absorbed")) == set()
    assert await wired.fake.smembers(wired.store.subject_parks_key("agent", "scope-m", "person", "survivor")) == {"m1"}
    # The person-thread key was re-keyed too.
    assert await wired.fake.smembers(
        wired.store.subject_parks_key("agent", "scope-m", "thread", "bridge:@person:survivor")
    ) == {"m1"}
    # The stored subject descriptor now names the survivor.
    import json

    from tai42_skeleton.interactions.store import serde

    raw = await wired.fake.hgetall(wired.store.state_key("m1"))
    descriptor = json.loads(serde.as_str(raw[b"subjects"] if b"subjects" in raw else raw["subjects"]))
    assert descriptor["by_kind"]["person"] == "survivor"
    assert descriptor["by_kind"]["thread"] == "bridge:@person:survivor"


# --- every teardown door routes through the kill seam ------------------------------------


async def test_cancel_door_routes_to_kill_park(wired, monkeypatch):
    from tai42_skeleton.operations import interactions as ops

    monkeypatch.setattr(ops, "client_ctx", _fake_ctx(wired.fake))
    monkeypatch.setattr(ops, "interactions_settings", lambda: wired.settings)
    calls: list[str] = []

    async def _spy(r, store, interaction_id, group_id, *, reason, act_on=None):
        calls.append(interaction_id)
        return "pruned"

    monkeypatch.setattr(ops, "kill_park", _spy)
    await _add_park(wired, iid="cd", delivery=None, run_delivery_id="rd-cd")
    await ops.cancel_interaction("cd")
    assert calls == ["cd"]


async def test_cancel_parks_for_thread_kills_via_both_indices(wired, monkeypatch, kill_handlers):
    # The thread/route-delete seam reaches a thread-bound park (thread reverse index) AND a
    # conversation park (subject index for kind="thread"), routing each through the kill seam.
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    record: list[tuple[str, str]] = []
    register_park_kill_handler(_recording_handler(record))
    # A background park bound to the thread (reverse index only, no subject context).
    await _add_park(
        wired,
        iid="bg",
        delivery=("deliver_tool", {"thread_id": "t1"}),
        run_delivery_id="rd-bg",
        thread_id="bridge:chat:z",
    )
    # A conversation park addressed to the thread subject.
    candidates = SubjectCandidates(target_kind="agent", target_name="scope-z", by_kind={"thread": "bridge:chat:z"})
    await _add_park(
        wired,
        iid="conv",
        delivery=("deliver_tool", {"thread_id": "t1"}),
        run_delivery_id="rd-conv",
        candidates=candidates,
    )

    reached = await cancel_parks_for_thread("bridge:chat:z", reason="route_deleted")

    assert set(reached) == {"bg", "conv"}
    assert {iid for iid, _r in record} == {"bg", "conv"}
    assert await wired.store.get_state(wired.fake, "bg") is None
    assert await wired.store.get_state(wired.fake, "conv") is None


async def test_cancel_parks_for_person_reaches_thread_and_person_subjects(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.interactions.helper import cancel_parks_for_person

    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    register_park_kill_handler(_recording_handler([]))
    # One park reachable only through the person subject, one only through the aggregated thread.
    person_c = SubjectCandidates(target_kind="agent", target_name="s", by_kind={"person": "pP"})
    thread_c = SubjectCandidates(target_kind="agent", target_name="s", by_kind={"thread": "bridge:@person:pP"})
    await _add_park(wired, iid="byperson", delivery=None, run_delivery_id="rd-p1", candidates=person_c)
    await _add_park(wired, iid="bythread", delivery=None, run_delivery_id="rd-p2", candidates=thread_c)

    reached = await cancel_parks_for_person("pP")

    assert set(reached) == {"byperson", "bythread"}
    assert await wired.store.get_state(wired.fake, "byperson") is None
    assert await wired.store.get_state(wired.fake, "bythread") is None
