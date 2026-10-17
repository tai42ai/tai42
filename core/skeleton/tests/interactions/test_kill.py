"""The kill: the one seam every withdrawal door routes through, and it ends the whole run.

A kill revokes the named park and every other park of its run (prune, ticket, channel, kill-due)
before any driver is told, then fires every driver's handler on each node of the run and delivers
the run's single terminal once through the platform ladder — durably, so whatever is not ready is
finished by the reaper and the door never fails once its own park is revoked. The four
conversation teardown doors (thread/person/route delete, person merge), the cancel doors and the
expiry reaper's ``on_expiry="kill"`` branch all reach it.
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
    DeliveryAddress,
    InteractionRequest,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_kit.interactions import park_kill as kit_park_kill
from tai42_kit.interactions.park_kill import register_park_kill_handler

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
    monkeypatch.setattr(kit_park_kill, "_park_kill_handlers", handlers)
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


def _wire_tools(monkeypatch, handlers: dict[str, Any], channels: Any = None) -> _Tools:
    tools = _Tools(handlers)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools, channels=channels))
    return tools


def _cid(run_delivery_id: str) -> str:
    return continuation_module._completion_id(run_delivery_id)


async def _add_park(
    wired,
    *,
    iid: str,
    gid: str = "kg",
    delivery: DeliveryAddress | None,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None = None,
    to: str = "user",
    thread_id: str | None = None,
    channel: str | None = None,
    recipient: str | None = None,
    chain_keys: list[str] | None = None,
    continuation_tool: str = "resume_tool",
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
        continuation_tool=continuation_tool,
        continuation_identity="svc-key",
        continuation_state_context=StateContext(door="conversation", candidates=candidates)
        if candidates is not None
        else None,
        expiry_at=deadline,
        run_delivery_id=run_delivery_id,
        delivery=delivery,
        channel=channel,
        recipient=recipient,
        chain_keys=chain_keys or [],
    )
    await wired.store.add(
        wired.fake,
        request,
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        thread_id=thread_id,
        to=to,  # type: ignore[arg-type]
        delivery=delivery,
        run_delivery_id=run_delivery_id,
    )


def _recording_handler(record: list[tuple[str, str]]):
    """A neutral driver that owns every node it is fired on and records each fire."""

    async def _handler(node: str, reason: str) -> bool:
        record.append((node, reason))
        return True

    return _handler


def _register(handler: Any, tool: str = "resume_tool") -> None:
    """Register ``handler`` as the driver that binds the ``tool`` continuation (the parks it owns carry it)."""
    register_park_kill_handler(handler, continuation_tool=tool)


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
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
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
    _register(_recording_handler(record))
    await _add_park(
        wired,
        iid="k1",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )

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
    _register(_recording_handler([]))
    await _add_park(
        wired,
        iid="nested",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-9",
        to="caller",
    )
    await kill_park(wired.fake, wired.store, "nested", "kg", reason="cancelled")
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 1
    assert fires[0]["arguments"]["completion_id"] == _cid("rd-9")


async def test_receiver_less_kill_subject_tracks_a_failed_outcome(wired, monkeypatch, kill_handlers):
    _wire_tools(monkeypatch, {})
    _register(_recording_handler([]))
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
    _register(_recording_handler([]))
    await _add_park(
        wired,
        iid="sib",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-3",
    )
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
    _register(_recording_handler(record))
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
    _register(_recording_handler(record))
    candidates = SubjectCandidates(target_kind="agent", target_name="scope-k", by_kind={"person": "pK"})
    await _answered_park_with_expired_state(wired, iid="agedk", run_delivery_id="rd-agedk", candidates=candidates)

    reached = await kill_module.kill_parks_for_subject("person", "pK", reason="person_erased")

    # The aged-out running entry is torn down and REPORTED reached, not silently omitted as "gone".
    assert reached == ["agedk"]
    assert record == [("agedk", "person_erased")]
    assert not await _kill_due_present(wired, "agedk")


# --- durability: a transient teardown raise keeps the kill-due for the reaper -------------


async def test_a_transient_handler_raise_keeps_the_kill_due_and_the_reaper_redelivers(
    wired, monkeypatch, kill_handlers, caplog
):
    import logging

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    fail_first = {"count": 0}

    async def _handler(node: str, reason: str) -> bool:
        fail_first["count"] += 1
        if fail_first["count"] == 1:
            raise RuntimeError("ancestor not torn down yet")
        return True

    _register(_handler)
    await _add_park(
        wired,
        iid="tr",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-5",
    )

    # The door's promise holds (the park is revoked); the raise is logged at ERROR, the kill-due
    # record is KEPT and the FAILED is not fired.
    with caplog.at_level(logging.ERROR):
        assert await kill_park(wired.fake, wired.store, "tr", "kg", reason="cancelled") == "pruned"
    assert any("tr" in rec.getMessage() and rec.levelno == logging.ERROR for rec in caplog.records)
    assert await wired.store.get_state(wired.fake, "tr") is None
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
    _register(_recording_handler(record))
    # Seed a durable kill-due record + due index member (the shape ``enqueue_kill`` commits).
    await wired.fake.hset(
        wired.store.kill_due_key("crash"),
        mapping={
            "reason": "cancelled",
            "terminal": PARK_COMPLETION_FAILED,
            "attempts": "0",
            "deadline_ms": str(int(datetime.now(UTC).timestamp() * 1000) + 3_600_000),
            "delivery": DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1").model_dump_json(),
            "run_delivery_id": "rd-6",
            "continuation_tool": "resume_tool",
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
    _register(_recording_handler([]))
    # A kill-due record already PAST its give-up deadline (the teardown never completed).
    await wired.fake.hset(
        wired.store.kill_due_key("dead"),
        mapping={
            "reason": "cancelled",
            "terminal": PARK_COMPLETION_FAILED,
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
    _register(_recording_handler(record))
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
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
    )
    await wired.store.add(
        wired.fake,
        request,
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-8",
    )

    assert await reaper_module.reap_expired_parks_once() == 1
    # The whole run was taken down and the run's door FAILED delivered.
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
    _register(_recording_handler(record))
    await _add_park(
        wired,
        iid="race",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-race",
    )
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
    _register(_recording_handler(killed))
    captured: list[str] = []

    async def _stub(
        identity,
        fingerprint,
        tool,
        interaction_id,
        answer,
        park_context=None,
        park_asked_by=(),
        caller_ask_landing=None,
        chain_keys=(),
        *,
        mark_detached=True,
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
    _register(_recording_handler(record))
    # A park addressed to person ``pE`` under a scope, reachable only through the subject index.
    candidates = SubjectCandidates(target_kind="agent", target_name="scope-x", by_kind={"person": "pE"})
    await _add_park(
        wired,
        iid="erased",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-e",
        candidates=candidates,
    )

    reached = await kill_module.kill_parks_for_subject("person", "pE", reason="person_erased")

    assert reached == ["erased"]
    assert record == [("erased", "person_erased")]
    assert await wired.store.get_state(wired.fake, "erased") is None


async def test_merge_rekeys_the_index_stored_context_and_thread_index(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.interactions.helper import rekey_park_subjects

    candidates = SubjectCandidates(
        target_kind="agent",
        target_name="scope-m",
        by_kind={"person": "absorbed", "thread": "bridge:@person:absorbed"},
    )
    await _add_park(wired, iid="m1", delivery=None, run_delivery_id="rd-m", candidates=candidates)

    await rekey_park_subjects(
        [("person", "absorbed", "survivor"), ("thread", "bridge:@person:absorbed", "bridge:@person:survivor")]
    )

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

    async def _spy(r, store, interaction_id, group_id, *, reason, act_on=None, terminal=None):
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
    _register(_recording_handler(record))
    # A background park bound to the thread (reverse index only, no subject context).
    await _add_park(
        wired,
        iid="bg",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-bg",
        thread_id="bridge:chat:z",
    )
    # A conversation park addressed to the thread subject.
    candidates = SubjectCandidates(target_kind="agent", target_name="scope-z", by_kind={"thread": "bridge:chat:z"})
    await _add_park(
        wired,
        iid="conv",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-conv",
        candidates=candidates,
    )

    reached = await cancel_parks_for_thread("bridge:chat:z", reason="route_deleted")

    assert set(reached) == {"bg", "conv"}
    assert {iid for iid, _r in record} == {"bg", "conv"}
    assert await wired.store.get_state(wired.fake, "bg") is None
    assert await wired.store.get_state(wired.fake, "conv") is None


async def test_cancel_parks_for_thread_also_reaches_the_extra_subjects(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    _register(_recording_handler([]))
    # One park reachable only through the person subject, one only through the aggregated thread.
    person_c = SubjectCandidates(target_kind="agent", target_name="s", by_kind={"person": "pP"})
    thread_c = SubjectCandidates(target_kind="agent", target_name="s", by_kind={"thread": "bridge:@person:pP"})
    await _add_park(wired, iid="byperson", delivery=None, run_delivery_id="rd-p1", candidates=person_c)
    await _add_park(wired, iid="bythread", delivery=None, run_delivery_id="rd-p2", candidates=thread_c)

    reached = await cancel_parks_for_thread(
        "bridge:@person:pP", also_subjects=[("person", "pP")], reason="person_erased"
    )

    assert set(reached) == {"byperson", "bythread"}
    assert await wired.store.get_state(wired.fake, "byperson") is None
    assert await wired.store.get_state(wired.fake, "bythread") is None


async def test_rekey_park_subjects_skips_a_move_onto_itself(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.interactions.helper import rekey_park_subjects

    candidates = SubjectCandidates(target_kind="agent", target_name="scope-s", by_kind={"person": "same"})
    await _add_park(wired, iid="s1", delivery=None, run_delivery_id="rd-s", candidates=candidates)
    await rekey_park_subjects([("person", "same", "same")])
    assert await wired.fake.smembers(wired.store.subject_parks_key("agent", "scope-s", "person", "same")) == {"s1"}


# --- the channel that delivered the ask is told; the door names its terminal -------------


class _StubChannel:
    """A neutral channel that records each withdrawal on a ledger it shares with the door's fires."""

    def __init__(self, ledger: list[tuple[str, Any]], *, fail_first: bool = False) -> None:
        self.ledger = ledger
        self.withdrawn: list[Any] = []
        self._fail_first = fail_first

    async def deliver(self, delivery: Any) -> None:
        return None

    async def notify(self, notification: Any) -> list[str]:
        return []

    async def withdraw(self, withdrawal: Any) -> None:
        self.withdrawn.append(withdrawal)
        self.ledger.append(("withdraw", withdrawal.interaction_id))
        if self._fail_first and len(self.withdrawn) == 1:
            from tai42_contract.channels import ChannelDeliveryError

            raise ChannelDeliveryError("store unreachable", retryable=True)


class _PlainChannel:
    """A neutral channel that keeps nothing withdrawable, so it omits the ``withdraw`` member."""

    async def deliver(self, delivery: Any) -> None:
        return None

    async def notify(self, notification: Any) -> list[str]:
        return []


def _channels(**channels: Any) -> Any:
    from tai42_skeleton.channels.registry import ChannelRegistry

    registry = ChannelRegistry()
    for name, channel in channels.items():
        registry.register(name, channel)
    return registry


def _ledger_tools(monkeypatch, ledger: list[tuple[str, Any]], channels: Any) -> _Tools:
    def _deliver(arguments: dict[str, Any]) -> None:
        ledger.append(("deliver", arguments["status"]))

    return _wire_tools(monkeypatch, {"deliver_tool": _deliver}, channels=channels)


_ADDRESS = DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1")


async def test_kill_withdraws_the_channel_delivery_before_firing_the_terminal(wired, monkeypatch, kill_handlers):
    from tai42_contract.channels import ChannelWithdrawal

    ledger: list[tuple[str, Any]] = []
    stub = _StubChannel(ledger)
    _ledger_tools(monkeypatch, ledger, _channels(stub=stub))
    _register(_recording_handler([]))
    await _add_park(wired, iid="k1", delivery=_ADDRESS, run_delivery_id="rd-w1", channel="stub", recipient="r1")

    assert await kill_park(wired.fake, wired.store, "k1", "kg", reason="cancelled") == "pruned"

    assert stub.withdrawn == [ChannelWithdrawal(interaction_id="k1", recipient="r1")]
    # The channel's hold is free before the door's terminal is fired.
    assert [entry[0] for entry in ledger] == ["withdraw", "deliver"]
    assert not await _kill_due_present(wired, "k1")


async def test_kill_of_a_run_less_park_withdraws_the_channel_delivery(wired, monkeypatch, kill_handlers):
    from tai42_contract.channels import ChannelWithdrawal

    ledger: list[tuple[str, Any]] = []
    stub = _StubChannel(ledger)
    _ledger_tools(monkeypatch, ledger, _channels(stub=stub))
    await _add_park(wired, iid="rl1", delivery=None, run_delivery_id=None, channel="stub", recipient="r1")

    assert await kill_park(wired.fake, wired.store, "rl1", "kg", reason="cancelled") == "pruned"

    assert stub.withdrawn == [ChannelWithdrawal(interaction_id="rl1", recipient="r1")]
    assert await wired.store.get_state(wired.fake, "rl1") is None
    # A run-less park has no run to deliver a terminal to.
    assert ledger == [("withdraw", "rl1")]


async def test_kill_without_a_channel_or_without_the_member_withdraws_nothing(wired, monkeypatch, kill_handlers):
    ledger: list[tuple[str, Any]] = []
    stub = _StubChannel(ledger)
    _ledger_tools(monkeypatch, ledger, _channels(stub=stub, plain=_PlainChannel()))
    _register(_recording_handler([]))
    await _add_park(wired, iid="inbox", delivery=_ADDRESS, run_delivery_id="rd-in")
    await _add_park(wired, iid="plain", delivery=_ADDRESS, run_delivery_id="rd-pl", channel="plain", recipient="r1")

    assert await kill_park(wired.fake, wired.store, "inbox", "kg", reason="cancelled") == "pruned"
    assert await kill_park(wired.fake, wired.store, "plain", "kg", reason="cancelled") == "pruned"

    # Neither ask reached a channel's ``withdraw``; both terminals were still delivered.
    assert stub.withdrawn == []
    assert [entry[0] for entry in ledger] == ["deliver", "deliver"]


async def test_kill_of_an_unknown_channel_raises_and_keeps_the_kill_due(wired, monkeypatch, kill_handlers):
    ledger: list[tuple[str, Any]] = []
    _ledger_tools(monkeypatch, ledger, _channels())
    _register(_recording_handler([]))
    await _add_park(wired, iid="gone1", delivery=_ADDRESS, run_delivery_id="rd-g", channel="gone", recipient="r1")

    with pytest.raises(ValueError, match="unknown channel"):
        await kill_park(wired.fake, wired.store, "gone1", "kg", reason="cancelled")

    assert await _kill_due_present(wired, "gone1")
    assert ledger == []


async def test_withdraw_raise_keeps_the_kill_due_and_the_redelivery_withdraws_and_delivers_once(
    wired, monkeypatch, kill_handlers
):
    from tai42_contract.channels import ChannelDeliveryError, ChannelWithdrawal
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    ledger: list[tuple[str, Any]] = []
    stub = _StubChannel(ledger, fail_first=True)
    _ledger_tools(monkeypatch, ledger, _channels(stub=stub))
    _register(_recording_handler([]))
    await _add_park(wired, iid="wr", delivery=_ADDRESS, run_delivery_id="rd-wr", channel="stub", recipient="r1")

    with pytest.raises(ChannelDeliveryError):
        await kill_park(wired.fake, wired.store, "wr", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)
    assert await _kill_due_present(wired, "wr")
    assert ledger == [("withdraw", "wr")]

    await wired.fake.zadd(wired.store.kill_due_index_key, {"wr": 0})
    assert await reaper_module.redeliver_due_kills_once() == 1

    # The redelivery read the channel facts and the terminal off the kill-due record.
    assert stub.withdrawn == [ChannelWithdrawal(interaction_id="wr", recipient="r1")] * 2
    assert ledger == [("withdraw", "wr"), ("withdraw", "wr"), ("deliver", PARK_COMPLETION_WITHDRAWN)]
    assert not await _kill_due_present(wired, "wr")


async def test_kill_due_record_carries_terminal_channel_and_recipient(wired, monkeypatch, kill_handlers):
    from tai42_contract.channels import ChannelDeliveryError
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    from tai42_skeleton.interactions.store import KillDue, serde

    ledger: list[tuple[str, Any]] = []
    _ledger_tools(monkeypatch, ledger, _channels(stub=_StubChannel(ledger, fail_first=True)))
    _register(_recording_handler([]))
    await _add_park(wired, iid="kd", delivery=_ADDRESS, run_delivery_id="rd-kd", channel="stub", recipient="r1")

    with pytest.raises(ChannelDeliveryError):
        await kill_park(wired.fake, wired.store, "kd", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)

    raw = await wired.fake.hgetall(wired.store.kill_due_key("kd"))
    fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
    assert fields["terminal"] == PARK_COMPLETION_WITHDRAWN
    assert fields["channel"] == "stub"
    assert fields["recipient"] == "r1"
    due = await wired.store.claim_kill_retry(wired.fake, "kd", datetime.now(UTC) + timedelta(hours=1), 1000, 60_000)
    assert isinstance(due, KillDue)
    assert (due.terminal, due.channel, due.recipient) == (PARK_COMPLETION_WITHDRAWN, "stub", "r1")


async def test_kill_due_without_terminal_is_a_loud_read_error(wired):
    await wired.fake.hset(
        wired.store.kill_due_key("noterm"),
        mapping={
            "reason": "cancelled",
            "attempts": "0",
            "deadline_ms": str(int(datetime.now(UTC).timestamp() * 1000) + 3_600_000),
            "run_delivery_id": "rd-nt",
        },
    )
    await wired.fake.zadd(wired.store.kill_due_index_key, {"noterm": 0})

    with pytest.raises(KeyError, match="terminal"):
        await wired.store.claim_kill_retry(wired.fake, "noterm", datetime.now(UTC), 1000, 60_000)


async def test_cancel_delivers_the_withdrawn_terminal(wired, monkeypatch, kill_handlers):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    _register(_recording_handler([]))
    await _add_park(wired, iid="cw", delivery=_ADDRESS, run_delivery_id="rd-cw")

    await kill_park(wired.fake, wired.store, "cw", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)

    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 1
    assert fires[0]["arguments"]["status"] == PARK_COMPLETION_WITHDRAWN
    assert fires[0]["arguments"]["completion_id"] == _cid("rd-cw")


async def test_withdrawn_receiver_less_kill_writes_no_waiting_outcome_but_leaves_the_subject_index(
    wired, monkeypatch, kill_handlers
):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN, InteractionResponse

    _wire_tools(monkeypatch, {})
    _register(_recording_handler([]))
    candidates = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pW"})
    await _add_park(wired, iid="rw", delivery=None, run_delivery_id="rd-rw", candidates=candidates)
    # Answered: the entry is now ``running`` and keeps its subject-index membership while it drives.
    now = datetime.now(UTC)
    await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="rw", answer="y", answered_by="op", answered_at=now),
        "kg",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )
    parks_key = wired.store.subject_parks_key("agent", "a", "person", "pW")
    assert await wired.fake.smembers(parks_key)

    await kill_park(wired.fake, wired.store, "rw", "kg", reason="person_erased", terminal=PARK_COMPLETION_WITHDRAWN)

    # A deliberate takedown has no outcome for anyone to take, and the run left the subject index.
    assert await wired.store.claim_outcome(wired.fake, _cid("rd-rw")) is None
    assert await wired.fake.smembers(parks_key) == set()


async def test_cancel_door_passes_the_withdrawn_terminal(wired, monkeypatch):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    from tai42_skeleton.operations import interactions as ops

    monkeypatch.setattr(ops, "client_ctx", _fake_ctx(wired.fake))
    monkeypatch.setattr(ops, "interactions_settings", lambda: wired.settings)
    terminals: list[Any] = []

    async def _spy(r, store, interaction_id, group_id, *, reason, act_on=None, terminal=None):
        terminals.append(terminal)
        return "pruned"

    monkeypatch.setattr(ops, "kill_park", _spy)
    await _add_park(wired, iid="cdw", delivery=None, run_delivery_id="rd-cdw")
    await ops.cancel_interaction("cdw")
    assert terminals == [PARK_COMPLETION_WITHDRAWN]


async def test_visit_cancel_passes_the_withdrawn_terminal(wired, monkeypatch):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    from tai42_skeleton.interactions import visit as visit_module

    monkeypatch.setattr(visit_module, "client_ctx", _fake_ctx(wired.fake))
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: wired.settings)
    calls: list[tuple[str, str, Any]] = []

    async def _spy(r, store, interaction_id, group_id, *, reason, act_on=None, terminal=None):
        calls.append((interaction_id, reason, terminal))
        return "pruned"

    monkeypatch.setattr(visit_module, "kill_park", _spy)
    await visit_module._cancel_all(wired.store, ["v1"], {"v1": {"group_id": "kg"}})
    assert calls == [("v1", "cancelled", PARK_COMPLETION_WITHDRAWN)]


async def test_cancel_parks_for_thread_passes_the_withdrawn_terminal(wired, monkeypatch, kill_handlers):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    from tai42_skeleton.interactions.helper import cancel_parks_for_thread

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    _register(_recording_handler([]))
    await _add_park(wired, iid="tw", delivery=_ADDRESS, run_delivery_id="rd-tw", thread_id="bridge:chat:w")

    assert await cancel_parks_for_thread("bridge:chat:w", reason="thread_deleted") == ["tw"]

    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert [f["arguments"]["status"] for f in fires] == [PARK_COMPLETION_WITHDRAWN]


async def test_expiry_kill_passes_the_failed_terminal(wired, monkeypatch, kill_handlers):
    terminals: list[Any] = []

    async def _spy(r, store, interaction_id, group_id, *, reason, act_on=None, terminal=None):
        terminals.append((reason, terminal))
        return "pruned"

    monkeypatch.setattr(reaper_module, "kill_park", _spy)
    monkeypatch.setattr(reaper_module, "_emit_ask_expired_unanswered", _noop_emit)
    past = datetime.now(UTC) - timedelta(seconds=1)
    request = InteractionRequest(
        interaction_id="ef",
        group_id="eg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("ef"),
        created_at=datetime.now(UTC),
        timeout_at=past,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=past,
        on_expiry="kill",
        run_delivery_id="rd-ef",
    )
    await wired.store.add(wired.fake, request, idle_ttl=86400, continuation_fingerprint="fp-1", run_delivery_id="rd-ef")

    assert await reaper_module.reap_expired_parks_once() == 1
    assert terminals == [("expired", PARK_COMPLETION_FAILED)]


async def _noop_emit(*_args: Any, **_kwargs: Any) -> None:
    return None


# --- a kill ends the whole run: every park of the run revoked, every node of it fired ----------

_K1 = "tai42:chained-park:k1"
_K2 = "tai42:chained-park:k2"


def _owning_handler(owned: set[str], seen: list[str], *, not_ready: set[str] | None = None):
    """A neutral driver that owns the nodes in ``owned`` and cannot finish yet on those in ``not_ready``."""
    pending = not_ready if not_ready is not None else set()

    async def _handler(node: str, reason: str) -> bool:
        seen.append(node)
        if node in pending:
            from tai42_kit.interactions.park_kill import ParkKillNotReadyError

            raise ParkKillNotReadyError(node, "a drive holds it")
        return node in owned

    return _handler


async def _removed_events(wired) -> dict[str, dict[str, Any]]:
    events = await wired.fake.xrange(wired.store.events_key)
    return {f["interaction_id"]: f for _id, f in events if f["type"] == "interaction.removed"}


async def _redeliver(wired, iid: str) -> int:
    """Make ``iid``'s kill-due record due now and run one reaper pass over the kill-due index."""
    await wired.fake.zadd(wired.store.kill_due_index_key, {iid: 0})
    return await reaper_module.redeliver_due_kills_once()


def _deliver_statuses(tools: _Tools) -> list[str]:
    return [c["arguments"]["status"] for c in tools.calls if c["key"] == "deliver_tool"]


async def test_a_kill_of_a_nested_park_takes_the_runs_own_ask_down_and_resumes_nothing(
    wired, monkeypatch, kill_handlers
):
    from tai42_contract.channels import ChannelWithdrawal
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    ledger: list[tuple[str, Any]] = []
    stub = _StubChannel(ledger)
    tools = _ledger_tools(monkeypatch, ledger, _channels(stub=stub))
    seen: list[str] = []
    _register(_owning_handler({"p1", _K1}, seen), tool="agent_resume")
    # p1 is the run's own ask; A is the ask of the run nested under the call p1's run waits on (K1).
    await _add_park(
        wired,
        iid="p1",
        delivery=_ADDRESS,
        run_delivery_id="rd-n",
        channel="stub",
        recipient="r1",
        continuation_tool="agent_resume",
    )
    await _add_park(
        wired,
        iid="A",
        delivery=_ADDRESS,
        run_delivery_id="rd-n",
        channel="stub",
        recipient="r2",
        chain_keys=[_K1],
        continuation_tool="child_resume",
    )

    result = await kill_park(wired.fake, wired.store, "A", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)

    assert result == "pruned"
    assert await wired.store.get_state(wired.fake, "p1") is None
    assert (await _removed_events(wired))["p1"]["reason"] == "cancelled"
    assert stub.withdrawn == [
        ChannelWithdrawal(interaction_id="A", recipient="r2"),
        ChannelWithdrawal(interaction_id="p1", recipient="r1"),
    ]
    # A's own completion first, then the caller waiting on its call, then the run's other park.
    assert seen == ["A", _K1, "p1"]
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert fires
    assert {f["arguments"]["status"] for f in fires} == {PARK_COMPLETION_WITHDRAWN}
    assert {f["arguments"]["completion_id"] for f in fires} == {_cid("rd-n")}
    assert not await _kill_due_present(wired, "A")
    assert not await _kill_due_present(wired, "p1")
    assert await wired.store.run_killed(wired.fake, "rd-n")


async def test_every_park_of_the_run_is_revoked_before_any_driver_is_told(wired, monkeypatch, kill_handlers):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN, RunTerminalFailed

    from .test_continuation_delivery_ladder import _drive, _fake_bind

    def _aborted_replay(_arguments: dict[str, Any]) -> None:
        raise RunTerminalFailed({"status": "aborted", "reason": "cancelled"})

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None, "resume_tool": _aborted_replay})
    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)
    observed: dict[str, Any] = {}

    async def _handler(node: str, reason: str) -> bool:
        if node == "a1":
            observed["b1_state"] = await wired.store.get_state(wired.fake, "b1")
            observed["b1_ticket"] = await wired.store.continuation_due_present(wired.fake, "b1")
            # The person's answer to b1 was claimed before the kill landed: its resume drives now, after
            # the driver ended the super-step, and lands on the aborted record.
            await _drive(
                wired, tool="resume_tool", interaction_id="b1", delivery=_ADDRESS, run_delivery_id="rd-r", ticket=False
            )
        return True

    _register(_handler)
    await _add_park(wired, iid="a1", delivery=_ADDRESS, run_delivery_id="rd-r")
    await _add_park(wired, iid="b1", delivery=_ADDRESS, run_delivery_id="rd-r")

    await kill_park(wired.fake, wired.store, "a1", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)

    assert observed == {"b1_state": None, "b1_ticket": False}
    assert PARK_COMPLETION_FAILED not in _deliver_statuses(tools)
    assert set(_deliver_statuses(tools)) == {PARK_COMPLETION_WITHDRAWN}


async def test_a_member_not_ready_does_not_fail_the_door(wired, monkeypatch, kill_handlers, caplog):
    import logging

    from tai42_contract.channels import ChannelWithdrawal
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    ledger: list[tuple[str, Any]] = []
    stub = _StubChannel(ledger)
    tools = _ledger_tools(monkeypatch, ledger, _channels(stub=stub))
    not_ready = {"A"}
    _register(_owning_handler({"p1", "A", _K1}, [], not_ready=not_ready))
    await _add_park(wired, iid="p1", delivery=_ADDRESS, run_delivery_id="rd-m", channel="stub", recipient="r1")
    await _add_park(wired, iid="A", delivery=_ADDRESS, run_delivery_id="rd-m", chain_keys=[_K1])

    with caplog.at_level(logging.WARNING):
        result = await kill_park(
            wired.fake, wired.store, "p1", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN
        )

    assert result == "pruned"
    assert await wired.store.get_state(wired.fake, "p1") is None
    assert stub.withdrawn == [ChannelWithdrawal(interaction_id="p1", recipient="r1")]
    assert _deliver_statuses(tools) == [PARK_COMPLETION_WITHDRAWN]
    assert not await _kill_due_present(wired, "p1")
    assert await _kill_due_present(wired, "A")
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING and "'A'" in rec.getMessage()]
    assert len(warnings) == 1

    not_ready.clear()
    assert await _redeliver(wired, "A") == 1
    assert not await _kill_due_present(wired, "A")


async def test_a_chained_call_nobody_owns_keeps_the_parks_record_until_it_is_owned(
    wired, monkeypatch, kill_handlers, caplog
):
    import logging

    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    seen: list[str] = []
    _register(_owning_handler({"A"}, seen))
    await _add_park(wired, iid="A", delivery=_ADDRESS, run_delivery_id="rd-c", chain_keys=[_K1])

    with caplog.at_level(logging.WARNING):
        assert (
            await kill_park(wired.fake, wired.store, "A", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)
            == "pruned"
        )
    assert await _kill_due_present(wired, "A")
    assert any(rec.levelno == logging.WARNING and _K1 in rec.getMessage() for rec in caplog.records)
    assert _deliver_statuses(tools) == []

    # The caller's driver records its wait on the call: now it owns the key.
    owner_seen: list[str] = []
    _register(_owning_handler({_K1}, owner_seen), tool="caller_resume")
    assert await _redeliver(wired, "A") == 1

    assert _K1 in owner_seen
    assert _deliver_statuses(tools) == [PARK_COMPLETION_WITHDRAWN]
    assert not await _kill_due_present(wired, "A")


async def _park_own_ask_under_its_own_routing(wired) -> str:
    """A run's tool body asks directly under the routing its dispatcher bound for the call; return the park id."""
    from tai42_contract.interactions import (
        ChainedResume,
        SuspendedInteraction,
        reset_chained_resume,
        reset_park_completion,
        reset_resume_continuation_tool,
        set_chained_resume,
        set_park_completion,
        set_resume_continuation_tool,
    )
    from tai42_contract.tools import tool_call_frame

    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
    from tai42_skeleton.authz.identity import CallerIdentity
    from tai42_skeleton.interactions import ask

    completion = set_park_completion("deliver_tool", thread_id="t1")
    run_face = set_resume_continuation_tool("resume_tool")
    identity = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        with tool_call_frame():
            routing = set_chained_resume(ChainedResume(delivery_tool="deliver_chained", chain_key=_K1, asked_by=("a",)))
            try:
                parked = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
            finally:
                reset_chained_resume(routing)
    finally:
        reset_execution_identity(identity)
        reset_resume_continuation_tool(run_face)
        reset_park_completion(completion)
    assert isinstance(parked, SuspendedInteraction)
    return parked.interaction_id


@pytest.mark.parametrize("door", ["cancel", "expiry"])
async def test_a_kill_of_a_park_adopted_by_the_run_that_minted_the_routing_completes_on_the_first_delivery(
    wired, monkeypatch, kill_handlers, door
):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    from tai42_skeleton.interactions.store import KILL_ACT_ON_PENDING

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    iid = await _park_own_ask_under_its_own_routing(wired)
    seen: list[str] = []
    _register(_owning_handler({iid}, seen))

    if door == "cancel":
        await kill_park(wired.fake, wired.store, iid, None, reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)
        terminal = PARK_COMPLETION_WITHDRAWN
    else:
        await kill_park(
            wired.fake,
            wired.store,
            iid,
            None,
            reason="expired",
            act_on=KILL_ACT_ON_PENDING,
            terminal=PARK_COMPLETION_FAILED,
        )
        terminal = PARK_COMPLETION_FAILED

    # The run's own ask records nothing of the call it was raised in: its driver owns the one node.
    assert seen == [iid]
    assert not await _kill_due_present(wired, iid)
    assert _deliver_statuses(tools) == [terminal]


async def test_a_park_whose_driver_has_not_persisted_keeps_its_record(wired, monkeypatch, kill_handlers, caplog):
    import logging

    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    owned: set[str] = set()
    _register(_owning_handler(owned, []))
    await _add_park(wired, iid="A", delivery=_ADDRESS, run_delivery_id="rd-np")

    with caplog.at_level(logging.WARNING):
        assert (
            await kill_park(wired.fake, wired.store, "A", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)
            == "pruned"
        )
    assert await _kill_due_present(wired, "A")
    assert any(rec.levelno == logging.WARNING and "'A'" in rec.getMessage() for rec in caplog.records)

    # The driver's persist lands: it owns the park now.
    owned.add("A")
    assert await _redeliver(wired, "A") == 1
    assert _deliver_statuses(tools) == [PARK_COMPLETION_WITHDRAWN]
    assert not await _kill_due_present(wired, "A")


async def test_the_visit_cancel_runs_start_when_a_member_is_not_ready(wired, monkeypatch, kill_handlers):
    from tai42_kit.utils.state_context import state_context

    from tai42_skeleton.interactions import visit as visit_module

    monkeypatch.setattr(visit_module, "client_ctx", _fake_ctx(wired.fake))
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: wired.settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)
    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    _register(_owning_handler({"p1", "A"}, [], not_ready={"A"}))
    candidates = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pV"})
    await _add_park(wired, iid="p1", delivery=_ADDRESS, run_delivery_id="rd-v", candidates=candidates, to="caller")
    await _add_park(wired, iid="A", delivery=_ADDRESS, run_delivery_id="rd-v", candidates=candidates, to="caller")

    async def _start(_extras: Any) -> dict[str, int]:
        return {"ok": 1}

    context = StateContext(door="conversation", candidates=candidates, actor="u-1", turn_id="t-1", inbound_id="in-1")
    with state_context(context):
        outcome = await visit_module.visit(target_name="a", cancel=["p1"], resume=[], start=_start, extras={})

    assert outcome.action == "started"
    assert outcome.result == {"ok": 1}
    assert await wired.store.get_state(wired.fake, "p1") is None
    assert await _kill_due_present(wired, "A")


async def test_the_cancel_door_answers_200_then_404_when_a_member_is_not_ready(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.operations import NotFoundError
    from tai42_skeleton.operations import interactions as ops

    monkeypatch.setattr(ops, "client_ctx", _fake_ctx(wired.fake))
    monkeypatch.setattr(ops, "interactions_settings", lambda: wired.settings)
    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    _register(_owning_handler({"p1", "A"}, [], not_ready={"A"}))
    await _add_park(wired, iid="p1", delivery=_ADDRESS, run_delivery_id="rd-cd2")
    await _add_park(wired, iid="A", delivery=_ADDRESS, run_delivery_id="rd-cd2")

    assert await ops.cancel_interaction("p1") == {"interaction_id": "p1", "status": "cancelled"}
    with pytest.raises(NotFoundError):
        await ops.cancel_interaction("p1")
    assert await _kill_due_present(wired, "A")


async def test_kill_walk_skips_a_run_member_that_reads_gone(wired, monkeypatch, kill_handlers):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    seen: list[str] = []
    _register(_owning_handler({"a1", "b1"}, seen))
    await _add_park(wired, iid="a1", delivery=_ADDRESS, run_delivery_id="rd-gn")
    await _add_park(wired, iid="b1", delivery=_ADDRESS, run_delivery_id="rd-gn")
    # b1's state aged out and no ticket stands: its run-index member names nothing live.
    await wired.fake.delete(wired.store.state_key("b1"))

    assert (
        await kill_park(wired.fake, wired.store, "a1", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)
        == "pruned"
    )

    assert seen == ["a1"]
    assert _deliver_statuses(tools) == [PARK_COMPLETION_WITHDRAWN]
    assert not await _kill_due_present(wired, "a1")
    assert not await _kill_due_present(wired, "b1")


async def test_kill_due_redelivery_walks_the_run_index(wired, monkeypatch, kill_handlers):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    _register(_recording_handler([]))
    await _add_park(wired, iid="a1", delivery=_ADDRESS, run_delivery_id="rd-rd")
    await _add_park(wired, iid="b1", delivery=_ADDRESS, run_delivery_id="rd-rd")
    # A crash right after a1's revoke MULTI: a1's kill-due record stands, b1 is still parked.
    await wired.store.enqueue_kill(
        wired.fake,
        "a1",
        "kg",
        delivery=_ADDRESS,
        run_delivery_id="rd-rd",
        subjects=None,
        chain_keys=[],
        reason="cancelled",
        kill_due_ttl=3600,
        first_attempt_at_ms=0,
        deadline_ms=int(datetime.now(UTC).timestamp() * 1000) + 3_600_000,
        terminal=PARK_COMPLETION_WITHDRAWN,
        continuation_tool="resume_tool",
    )
    assert await wired.store.get_state(wired.fake, "b1") is not None

    assert await _redeliver(wired, "a1") == 1

    assert await wired.store.get_state(wired.fake, "b1") is None
    assert set(_deliver_statuses(tools)) == {PARK_COMPLETION_WITHDRAWN}
    assert not await _kill_due_present(wired, "a1")
    assert not await _kill_due_present(wired, "b1")


async def test_expiry_kill_takes_down_the_whole_run_with_the_failed_terminal(wired, monkeypatch, kill_handlers):
    from tai42_skeleton.interactions.store import KILL_ACT_ON_PENDING

    tools = _wire_tools(monkeypatch, {})
    seen: list[str] = []
    _register(_owning_handler({"a1", "b1"}, seen))
    candidates = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pX2"})
    await _add_park(wired, iid="a1", delivery=None, run_delivery_id="rd-ex", candidates=candidates)
    await _add_park(wired, iid="b1", delivery=None, run_delivery_id="rd-ex", candidates=candidates)

    await kill_park(
        wired.fake,
        wired.store,
        "a1",
        "kg",
        reason="expired",
        act_on=KILL_ACT_ON_PENDING,
        terminal=PARK_COMPLETION_FAILED,
    )

    assert await wired.store.get_state(wired.fake, "b1") is None
    assert seen == ["a1", "b1"]
    assert tools.calls == []
    # One run, one terminal: the waiting outcome is keyed by the run's completion id and written once.
    outcome = await wired.store.claim_outcome(wired.fake, _cid("rd-ex"))
    assert outcome is not None
    assert outcome.status == "failed"
    assert await wired.store.claim_outcome(wired.fake, _cid("rd-ex")) is None


async def test_lineage_keys_are_fired_once_per_kill_and_again_on_redelivery(wired, monkeypatch, kill_handlers):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    seen: list[str] = []
    not_ready = {"b1"}
    _register(_owning_handler({"a1", "b1", _K1}, seen, not_ready=not_ready))
    # Two asks of the run nested under the same call: they share the lineage key.
    await _add_park(wired, iid="a1", delivery=_ADDRESS, run_delivery_id="rd-lk", chain_keys=[_K1])
    await _add_park(wired, iid="b1", delivery=_ADDRESS, run_delivery_id="rd-lk", chain_keys=[_K1])

    await kill_park(wired.fake, wired.store, "a1", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)

    assert seen == ["a1", _K1, "b1"]
    assert await _kill_due_present(wired, "b1")

    not_ready.clear()
    assert await _redeliver(wired, "b1") == 1

    assert seen == ["a1", _K1, "b1", "b1", _K1]
    assert not await _kill_due_present(wired, "b1")


async def test_a_grandchild_is_reached_through_the_run_index_and_its_lineage_keys_are_fired(
    wired, monkeypatch, kill_handlers
):
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    seen: list[str] = []
    _register(_owning_handler({"p1", "A", _K1, _K2}, seen))
    await _add_park(wired, iid="p1", delivery=_ADDRESS, run_delivery_id="rd-g2")
    # A is nested under K1 -> K2: the intermediate run owns no platform park of its own.
    await _add_park(wired, iid="A", delivery=_ADDRESS, run_delivery_id="rd-g2", chain_keys=[_K1, _K2])

    await kill_park(wired.fake, wired.store, "p1", "kg", reason="cancelled", terminal=PARK_COMPLETION_WITHDRAWN)

    assert await wired.store.get_state(wired.fake, "A") is None
    # The nearest caller first: the run nested deepest waits on K2, its caller on K1.
    assert seen == ["p1", "A", _K2, _K1]
    assert not await _kill_due_present(wired, "p1")
    assert not await _kill_due_present(wired, "A")
