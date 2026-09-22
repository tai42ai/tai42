"""The shared visit's pre-check, cancel, take, start and normalisation.

These cover the parts of the visit order that never drive a continuation: the structural
pre-check (every refusal raises with NOTHING cancelled and NOTHING resumed), the cancel and take
actions, the start-path normalisation, and the door state-binding scoping. The inline-resume drive
(a REAL continuation tool through ``run_tool``) is exercised in ``test_visit_resume.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    AnswerMismatchError,
    InteractionRequest,
    ParkableRunFailedError,
    ParkedEntryGoneError,
    ResumeItem,
    SuspendedInteraction,
    TakeItem,
    VisitRequestError,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.states.binding import StateAttach, StateBinding
from tai42_contract.template import TemplatedText
from tai42_contract.tools import current_tool_invocation
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.runs.chokepoint import collect_resumed_interactions


def _candidates(target_name: str = "a", **by_kind: str) -> SubjectCandidates:
    return SubjectCandidates(target_kind="agent", target_name=target_name, by_kind=dict(by_kind or {"person": "pA"}))


def _context(candidates: SubjectCandidates) -> StateContext:
    return StateContext(door="conversation", candidates=candidates, actor="u-1", turn_id="t-1", inbound_id="in-1")


def _park(
    store: InteractionStore,
    iid: str,
    *,
    gid: str = "g1",
    fmt: AnswerFormat = AnswerFormat.TEXT,
    candidates: SubjectCandidates | None = None,
    asked_by: list[str] | None = None,
    continuation_tool: str = "resume_tool",
) -> InteractionRequest:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=60)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="proceed?",
        answer_format=fmt,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool=continuation_tool,
        continuation_identity="svc-key",
        continuation_state_context=_context(candidates) if candidates is not None else None,
        expiry_at=expiry,
        asked_by=asked_by or [],
    )


@pytest.fixture(autouse=True)
def _wired(monkeypatch, fake_redis, fake_client_ctx):
    """Point the visit + continuation store seams at the shared fake and mark the store configured."""
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    settings = InteractionsSettings()
    monkeypatch.setattr(visit_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)
    return settings


@pytest.fixture
def store(_wired) -> InteractionStore:
    return InteractionStore(_wired.key_prefix)


async def _seed_caller(store: InteractionStore, fake_redis, iid: str, **kwargs) -> None:
    cands = kwargs.pop("candidates", _candidates())
    await store.add(fake_redis, _park(store, iid, candidates=cands, **kwargs), idle_ttl=86400, to="caller")


async def _seed_user(store: InteractionStore, fake_redis, iid: str, **kwargs) -> None:
    cands = kwargs.pop("candidates", _candidates())
    await store.add(fake_redis, _park(store, iid, candidates=cands, **kwargs), idle_ttl=86400, to="user")


async def _seed_outcome(store: InteractionStore, fake_redis, cid: str, status: str, result) -> None:
    await store.add_outcome(
        fake_redis,
        completion_id=cid,
        interaction_id=cid,
        status=status,  # type: ignore[arg-type]
        result=result,
        candidates=_candidates(),
        retention_ttl=3600,
    )


async def _is_parked(store: InteractionStore, fake_redis, iid: str) -> bool:
    entries = await store.list_parked_for(fake_redis, _candidates())
    return any(entry["id"] == iid for entry in entries)


# --- cancel + start + none -------------------------------------------------------------


async def test_cancel_only_tears_down_and_reports_none(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")
    with state_context(_context(_candidates())):
        outcome = await visit_module.visit(target_name="t", cancel=["i1"], resume=[], start=None, extras={})
    assert outcome.action == "none"
    assert outcome.cancelled == ["i1"]
    assert outcome.kind == "none"
    assert not await _is_parked(store, fake_redis, "i1")


async def test_cancel_and_start(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")

    async def _start(extras):
        return {"ok": 1}

    with state_context(_context(_candidates())):
        outcome = await visit_module.visit(target_name="t", cancel=["i1"], resume=[], start=_start, extras={})
    assert outcome.action == "started"
    assert outcome.cancelled == ["i1"]
    assert outcome.kind == "result"
    assert outcome.result == {"ok": 1}
    assert not await _is_parked(store, fake_redis, "i1")


async def test_start_only_returns_result():
    async def _start(extras):
        return {"r": 2}

    outcome = await visit_module.visit(target_name="t", cancel=[], resume=[], start=_start, extras={})
    assert outcome.action == "started"
    assert outcome.kind == "result"
    assert outcome.result == {"r": 2}


async def test_start_returns_user_park_is_parked():
    async def _start(extras):
        return SuspendedInteraction(interaction_id="u9", caller_interaction_ids=[])

    outcome = await visit_module.visit(target_name="t", cancel=[], resume=[], start=_start, extras={})
    assert outcome.action == "started"
    assert outcome.kind == "parked"
    assert outcome.suspended is not None
    assert outcome.suspended.interaction_id == "u9"


async def test_start_returns_caller_asks(store, fake_redis):
    await _seed_caller(store, fake_redis, "c1")

    async def _start(extras):
        return SuspendedInteraction(interaction_id="c1", interaction_ids=["c1"], caller_interaction_ids=["c1"])

    with state_context(_context(_candidates())):
        outcome = await visit_module.visit(target_name="t", cancel=[], resume=[], start=_start, extras={})
    assert outcome.action == "started"
    assert outcome.kind == "asks"
    assert [entry.id for entry in outcome.asks] == ["c1"]
    assert outcome.asks[0].to == "caller"
    # The asks outcome ALSO carries the re-park sentinel over every still-open id of the step, so a
    # caller passing the step up hands the sentinel itself back.
    assert outcome.suspended is not None
    assert outcome.suspended.interaction_ids == ["c1"]
    assert outcome.suspended.caller_interaction_ids == ["c1"]
    assert outcome.suspended.resume_owner == "resume_tool"


async def test_asks_outcome_sentinel_splits_a_mixed_step(store, fake_redis):
    # A step with BOTH a caller and a user ask open → the asks outcome returns the caller entries and
    # a sentinel over ALL open ids, with the caller subset split into caller_interaction_ids.
    await _seed_caller(store, fake_redis, "c1")
    await _seed_user(store, fake_redis, "u2")

    async def _start(extras):
        return SuspendedInteraction(interaction_id="c1", interaction_ids=["c1", "u2"], caller_interaction_ids=["c1"])

    with state_context(_context(_candidates())):
        outcome = await visit_module.visit(target_name="t", cancel=[], resume=[], start=_start, extras={})
    assert outcome.kind == "asks"
    assert [entry.id for entry in outcome.asks] == ["c1"]
    assert outcome.suspended is not None
    assert outcome.suspended.interaction_ids == ["c1", "u2"]
    assert outcome.suspended.caller_interaction_ids == ["c1"]
    assert outcome.suspended.resume_owner == "resume_tool"


async def test_asks_outcome_sentinel_raises_on_differing_resume_owners(store, fake_redis):
    # One super-step has one resume owner; differing owners across its open ids is a corrupt step.
    await _seed_caller(store, fake_redis, "c1")
    await _seed_user(store, fake_redis, "u2", continuation_tool="other_tool")

    async def _start(extras):
        return SuspendedInteraction(interaction_id="c1", interaction_ids=["c1", "u2"], caller_interaction_ids=["c1"])

    with state_context(_context(_candidates())), pytest.raises(RuntimeError, match="differing resume owners"):
        await visit_module.visit(target_name="t", cancel=[], resume=[], start=_start, extras={})


async def test_nothing_is_none():
    outcome = await visit_module.visit(target_name="t", cancel=[], resume=[], start=None, extras={})
    assert outcome.action == "none"
    assert outcome.kind == "none"
    assert outcome.cancelled == []


# --- the structural pre-check: every refusal leaves NOTHING done -----------------------


async def test_cancel_resume_overlap_raises_nothing_done(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t", cancel=["i1"], resume=[ResumeItem(id="i1", payload="x")], start=None, extras={}
        )
    assert exc.value.rule == "cancel_resume_overlap"
    assert await _is_parked(store, fake_redis, "i1")  # nothing cancelled


async def test_bad_id_raises_gone(store, fake_redis):
    with state_context(_context(_candidates())), pytest.raises(ParkedEntryGoneError):
        await visit_module.visit(target_name="t", cancel=["nope"], resume=[], start=None, extras={})


async def test_resume_with_extras_raises(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="i1", payload="x")], start=None, extras={"k": 1}
        )
    assert exc.value.rule == "resume_extras"
    assert await _is_parked(store, fake_redis, "i1")


async def test_resume_answer_mismatch_raises_nothing_claimed(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1", fmt=AnswerFormat.CONFIRM)
    with state_context(_context(_candidates())), pytest.raises(AnswerMismatchError):
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="i1", payload="not-a-bool")], start=None, extras={}
        )
    entries = {entry["id"]: entry for entry in await store.list_parked_for(fake_redis, _candidates())}
    assert entries["i1"]["status"] == "asking"  # nothing claimed


async def test_multi_run_across_two_steps_raises(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1", gid="g1", asked_by=["main"])
    await _seed_caller(store, fake_redis, "i2", gid="g2", asked_by=["other"])
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload="x"), ResumeItem(id="i2", payload="y")],
            start=None,
            extras={},
        )
    assert exc.value.rule == "multi_run"


async def test_payload_on_finished_entry_raises(store, fake_redis):
    await _seed_outcome(store, fake_redis, "cid", "finished", {"v": 1})
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="cid", payload="x")], start=None, extras={}
        )
    assert exc.value.rule == "finished_payload"


async def test_payload_on_failed_entry_raises(store, fake_redis):
    # A ``failed`` waiting outcome is settled just like a ``finished`` one: a resume PAYLOAD cannot
    # re-drive it, so the pre-check refuses it under the same ``finished_payload`` rule.
    await _seed_outcome(store, fake_redis, "cid", "failed", {"error": "boom"})
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="cid", payload="x")], start=None, extras={}
        )
    assert exc.value.rule == "finished_payload"


async def test_user_ask_in_resume_raises(store, fake_redis):
    await _seed_user(store, fake_redis, "u1")
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="u1", payload="x")], start=None, extras={}
        )
    assert exc.value.rule == "user_ask_in_resume"


async def test_take_and_resume_in_one_visit_raises_multi_run(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")
    await _seed_outcome(store, fake_redis, "cid", "finished", {"v": 1})
    with state_context(_context(_candidates())), pytest.raises(VisitRequestError) as exc:
        await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload="x"), TakeItem(id="cid")],
            start=None,
            extras={},
        )
    assert exc.value.rule == "multi_run"


# --- take ------------------------------------------------------------------------------


async def test_take_finished_returns_result(store, fake_redis):
    await _seed_outcome(store, fake_redis, "cid", "finished", {"v": 7})
    with state_context(_context(_candidates())):
        outcome = await visit_module.visit(
            target_name="t", cancel=[], resume=[TakeItem(id="cid")], start=None, extras={}
        )
    assert outcome.action == "taken"
    assert outcome.kind == "result"
    assert outcome.result == {"v": 7}
    assert await store.claim_outcome(fake_redis, "cid") is None  # already taken


async def test_take_failed_raises_into_the_door(store, fake_redis):
    await _seed_outcome(store, fake_redis, "cid", "failed", {"error": "boom"})
    with state_context(_context(_candidates())), pytest.raises(ParkableRunFailedError) as exc:
        await visit_module.visit(target_name="t", cancel=[], resume=[TakeItem(id="cid")], start=None, extras={})
    assert exc.value.error == {"error": "boom"}


# --- the resumed-interaction record note -----------------------------------------------


async def test_a_taken_id_is_noted_on_the_ambient_record(store, fake_redis):
    # An atomically claimed (taken) waiting outcome appends its id to the ambient run
    # record's resumed-interaction collector.
    await _seed_outcome(store, fake_redis, "cid", "finished", {"v": 7})
    with state_context(_context(_candidates())), collect_resumed_interactions() as resumed:
        await visit_module.visit(target_name="t", cancel=[], resume=[TakeItem(id="cid")], start=None, extras={})
    assert resumed == ["cid"]


async def test_a_refused_visit_notes_nothing(store, fake_redis):
    # A pre-check refusal raises before any resume/take acts, so no id is recorded — the
    # collector stays empty.
    await _seed_user(store, fake_redis, "u1")
    with (
        state_context(_context(_candidates())),
        collect_resumed_interactions() as resumed,
        pytest.raises(VisitRequestError),
    ):
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="u1", payload="x")], start=None, extras={}
        )
    assert resumed == []


# --- door state-binding scoping (deposited ONLY around start) --------------------------


async def test_state_binding_is_deposited_around_start_only():
    binding = StateBinding(states=[StateAttach(state="s", subject_expr=TemplatedText(content=".c"))])
    seen: dict[str, object] = {}

    async def _start(extras):
        invocation = current_tool_invocation()
        seen["tool_name"] = invocation.tool_name if invocation is not None else None
        seen["binding"] = invocation.state_binding if invocation is not None else None
        return {"ok": 1}

    outcome = await visit_module.visit(
        target_name="tgt", cancel=[], resume=[], start=_start, extras={}, state_binding=binding
    )
    assert outcome.action == "started"
    assert seen["tool_name"] == "tgt"
    assert seen["binding"] is binding
    # Reset after the visit: no ambient invocation lingers.
    assert current_tool_invocation() is None


async def test_no_binding_deposits_nothing_around_start():
    seen: dict[str, object] = {}

    async def _start(extras):
        seen["invocation"] = current_tool_invocation()
        return 1

    await visit_module.visit(target_name="tgt", cancel=[], resume=[], start=_start, extras={}, state_binding=None)
    assert seen["invocation"] is None


# --- the generic facade wrappers -------------------------------------------------------


async def test_list_parked_returns_entries(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")
    await _seed_caller(store, fake_redis, "i2", gid="g2")
    with state_context(_context(_candidates())):
        entries = await visit_module.list_parked()
    assert sorted(entry.id for entry in entries) == ["i1", "i2"]
    assert all(entry.to == "caller" for entry in entries)
    assert all(entry.on_expiry == "kill" for entry in entries)


async def test_resume_parked_take_finished(store, fake_redis):
    await _seed_outcome(store, fake_redis, "cid", "finished", {"v": 9})
    with state_context(_context(_candidates())):
        outcome = await visit_module.resume_parked("cid")
    assert outcome.action == "taken"
    assert outcome.result == {"v": 9}


async def test_cancel_parked_tears_down(store, fake_redis):
    await _seed_caller(store, fake_redis, "i1")
    with state_context(_context(_candidates())):
        outcome = await visit_module.cancel_parked(["i1"])
    assert outcome.cancelled == ["i1"]
    assert not await _is_parked(store, fake_redis, "i1")
