"""The shared visit's ``on_act`` notice: heard at each point the visit acts, and nowhere else.

A notice is awaited before the cancels, before and after each resume or take claim, and before the
start; never during the pre-check or the run-entry drain. ``settle_visit_claim`` reads whether a
claim's entries are still open, and nothing more.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    AnswerMismatchError,
    InteractionRequest,
    InteractionResponse,
    ParkableRunFailedError,
    ParkedEntryGoneError,
    ResumeItem,
    TakeItem,
    VisitAct,
    VisitRequestError,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.app import instance
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.manifest import Manifest

_MOD = "tests.app._fixtures.resume_tool"


def _candidates() -> SubjectCandidates:
    return SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA"})


def _context() -> StateContext:
    return StateContext(door="conversation", candidates=_candidates(), actor="u-1", turn_id="t-1")


def _park(store: InteractionStore, iid: str, *, gid: str = "g1", fmt: AnswerFormat = AnswerFormat.FREE):
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
        to="caller",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=_context(),
        expiry_at=expiry,
    )


@pytest.fixture(autouse=True)
def _wired(monkeypatch, fake_redis, fake_client_ctx):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    settings = InteractionsSettings()
    monkeypatch.setattr(visit_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)

    @asynccontextmanager
    async def _fake_bind(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)
    return settings


@pytest.fixture
def store(_wired) -> InteractionStore:
    return InteractionStore(_wired.key_prefix)


async def _clear_server() -> None:
    provider = instance.app._fast_mcp.local_provider
    for tool in list(await provider.list_tools()):
        provider.remove_tool(tool.name)


@pytest.fixture(autouse=True)
def _clean_server():
    asyncio.run(_clear_server())
    yield
    asyncio.run(_clear_server())


@asynccontextmanager
async def _app():
    await _clear_server()
    async with instance.app.app_context(Manifest.model_validate({"tools": [{"title": "resume", "module": _MOD}]})):
        with state_context(_context()):
            yield


async def _seed(store: InteractionStore, fake_redis, iid: str, **kwargs: Any) -> None:
    await store.add(fake_redis, _park(store, iid, **kwargs), idle_ttl=86400, to="caller")


async def _seed_outcome(store: InteractionStore, fake_redis, cid: str, status: str = "finished") -> None:
    await store.add_outcome(
        fake_redis,
        completion_id=cid,
        interaction_id=cid,
        status=status,  # type: ignore[arg-type]
        result={"error": "upstream refused"} if status == "failed" else {"v": 1},
        candidates=_candidates(),
        retention_ttl=3600,
    )


async def _status(store: InteractionStore, fake_redis, iid: str) -> str | None:
    state = await store.get_state(fake_redis, iid)
    return None if state is None else state.status


async def _due_present(store: InteractionStore, fake_redis, iid: str) -> bool:
    return bool(await fake_redis.hgetall(store.continuation_due_key(iid)))


class _Log:
    """One ordered log of the notices ``on_act`` hears and the store claims the visit makes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []
        self.raise_on: tuple[str, str] | None = None

    async def on_act(self, act: VisitAct) -> None:
        self.events.append((act.action, act.phase, *act.interaction_ids))
        if self.raise_on == (act.action, act.phase):
            raise ConnectionError(f"the {act.action}/{act.phase} notice failed")

    @property
    def notices(self) -> list[tuple[str, ...]]:
        return [event for event in self.events if event[0] != "claim"]


@pytest.fixture
def log(monkeypatch) -> _Log:
    recorded = _Log()
    record_answer = InteractionStore.record_answer
    claim_outcome = InteractionStore.claim_outcome

    async def _record_answer(self, r, response, *args, **kwargs):
        recorded.events.append(("claim", response.interaction_id))
        return await record_answer(self, r, response, *args, **kwargs)

    async def _claim_outcome(self, r, cid):
        recorded.events.append(("claim", cid))
        return await claim_outcome(self, r, cid)

    monkeypatch.setattr(InteractionStore, "record_answer", _record_answer)
    monkeypatch.setattr(InteractionStore, "claim_outcome", _claim_outcome)
    return recorded


async def _started(extras) -> dict[str, Any]:
    return {"ran": True}


# --- the order of the notices ----------------------------------------------------------


async def test_a_cancel_and_start_hears_cancel_then_start(store, fake_redis, log) -> None:
    await _seed(store, fake_redis, "i1")
    with state_context(_context()):
        outcome = await visit_module.visit(
            target_name="t", cancel=["i1"], resume=[], start=_started, extras={}, on_act=log.on_act
        )
    assert outcome.action == "started"
    assert log.notices == [("cancel", "before", "i1"), ("start", "before")]


async def test_each_sibling_resume_is_heard_around_its_claim(store, fake_redis, log) -> None:
    await _seed(store, fake_redis, "i1")
    await _seed(store, fake_redis, "i2")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"value": 1}), ResumeItem(id="i2", payload={"value": 2})],
            start=None,
            extras={},
            on_act=log.on_act,
        )
    assert outcome.action == "resumed"
    assert log.events == [
        ("resume", "before", "i1"),
        ("claim", "i1"),
        ("resume", "committed", "i1"),
        ("resume", "before", "i2"),
        ("claim", "i2"),
        ("resume", "committed", "i2"),
    ]


async def test_each_take_is_heard_around_its_claim(store, fake_redis, log) -> None:
    await _seed_outcome(store, fake_redis, "c1")
    await _seed_outcome(store, fake_redis, "c2")
    with state_context(_context()):
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[TakeItem(id="c1"), TakeItem(id="c2")],
            start=None,
            extras={},
            on_act=log.on_act,
        )
    assert outcome.action == "taken"
    assert log.events == [
        ("take", "before", "c1"),
        ("claim", "c1"),
        ("take", "committed", "c1"),
        ("take", "before", "c2"),
        ("claim", "c2"),
        ("take", "committed", "c2"),
    ]


async def test_a_failed_outcome_take_is_heard_committed_before_it_raises(store, fake_redis, log) -> None:
    await _seed_outcome(store, fake_redis, "c1", status="failed")
    with state_context(_context()), pytest.raises(ParkableRunFailedError):
        await visit_module.visit(
            target_name="t", cancel=[], resume=[TakeItem(id="c1")], start=None, extras={}, on_act=log.on_act
        )
    assert log.notices == [("take", "before", "c1"), ("take", "committed", "c1")]


# --- where no notice is heard ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"cancel": ["nope"], "resume": []}, ParkedEntryGoneError),
        ({"cancel": ["i1"], "resume": [ResumeItem(id="i1", payload="x")]}, VisitRequestError),
        ({"cancel": [], "resume": [ResumeItem(id="i1", payload="x")], "extras": {"k": 1}}, VisitRequestError),
        ({"cancel": [], "resume": [ResumeItem(id="i1", payload="x"), TakeItem(id="c1")]}, VisitRequestError),
        ({"cancel": [], "resume": [ResumeItem(id="t1", payload=7)]}, AnswerMismatchError),
        ({"cancel": [], "resume": [TakeItem(id="i1")]}, VisitRequestError),
    ],
)
async def test_a_pre_check_refusal_hears_nothing(kwargs, error, store, fake_redis, log) -> None:
    await _seed(store, fake_redis, "i1")
    await _seed(store, fake_redis, "t1", gid="g2", fmt=AnswerFormat.TEXT)
    await _seed_outcome(store, fake_redis, "c1")
    with state_context(_context()), pytest.raises(error):
        await visit_module.visit(target_name="t", start=_started, on_act=log.on_act, **{"extras": {}, **kwargs})
    assert log.events == []
    assert await _status(store, fake_redis, "i1") == "pending"


async def test_a_raising_run_entry_drain_hears_nothing(store, fake_redis, log, monkeypatch) -> None:
    await _seed(store, fake_redis, "i1")

    @asynccontextmanager
    async def _held():
        raise TimeoutError("the subject's pending saves did not finish")
        yield

    monkeypatch.setattr(visit_module, "run_entry_drain", _held)
    with state_context(_context()), pytest.raises(TimeoutError):
        await visit_module.visit(
            target_name="t", cancel=["i1"], resume=[], start=_started, extras={}, on_act=log.on_act
        )
    assert log.events == []


async def test_a_visit_that_does_nothing_hears_nothing(log) -> None:
    with state_context(_context()):
        outcome = await visit_module.visit(
            target_name="t", cancel=[], resume=[], start=None, extras={}, on_act=log.on_act
        )
    assert outcome.action == "none"
    assert log.events == []


async def test_a_claim_that_loses_its_race_hears_before_and_no_committed(store, fake_redis, log, monkeypatch) -> None:
    await _seed(store, fake_redis, "i1")

    async def _lost(self, r, response, *args, **kwargs):
        log.events.append(("claim", response.interaction_id))
        return False

    monkeypatch.setattr(InteractionStore, "record_answer", _lost)
    async with _app():
        with pytest.raises(ParkedEntryGoneError, match="resolved by another caller"):
            await visit_module.visit(
                target_name="t",
                cancel=[],
                resume=[ResumeItem(id="i1", payload={"value": 1})],
                start=None,
                extras={},
                on_act=log.on_act,
            )
    assert log.events == [("resume", "before", "i1"), ("claim", "i1")]


async def test_a_take_that_loses_its_race_hears_before_and_no_committed(store, fake_redis, log) -> None:
    await _seed_outcome(store, fake_redis, "c1")
    with state_context(_context()):
        # Another taker claims the outcome after the pre-check read and before this claim.
        async def _taken_first(act: VisitAct) -> None:
            await log.on_act(act)
            if act.phase == "before":
                await store.claim_outcome(fake_redis, "c1")

        with pytest.raises(ParkedEntryGoneError, match="already taken"):
            await visit_module.visit(
                target_name="t", cancel=[], resume=[TakeItem(id="c1")], start=None, extras={}, on_act=_taken_first
            )
    assert log.notices == [("take", "before", "c1")]


# --- a notice that raises --------------------------------------------------------------


async def test_a_raising_cancel_notice_cancels_and_starts_nothing(store, fake_redis, log) -> None:
    await _seed(store, fake_redis, "i1")
    log.raise_on = ("cancel", "before")
    started: list = []

    async def _start(extras):
        started.append(extras)

    with state_context(_context()), pytest.raises(ConnectionError):
        await visit_module.visit(target_name="t", cancel=["i1"], resume=[], start=_start, extras={}, on_act=log.on_act)
    assert started == []
    assert await _status(store, fake_redis, "i1") == "pending"


async def test_a_raising_resume_before_notice_leaves_the_entry_pending(store, fake_redis, log) -> None:
    await _seed(store, fake_redis, "i1")
    log.raise_on = ("resume", "before")
    async with _app():
        with pytest.raises(ConnectionError):
            await visit_module.visit(
                target_name="t",
                cancel=[],
                resume=[ResumeItem(id="i1", payload={"value": 1})],
                start=None,
                extras={},
                on_act=log.on_act,
            )
    assert ("claim", "i1") not in log.events
    assert await _status(store, fake_redis, "i1") == "pending"
    assert not await _due_present(store, fake_redis, "i1")


async def test_a_raising_committed_notice_leaves_the_claim_and_its_due_record(store, fake_redis, log) -> None:
    await _seed(store, fake_redis, "i1")
    log.raise_on = ("resume", "committed")
    async with _app():
        with pytest.raises(ConnectionError):
            await visit_module.visit(
                target_name="t",
                cancel=[],
                resume=[ResumeItem(id="i1", payload={"value": 1})],
                start=None,
                extras={},
                on_act=log.on_act,
            )
    assert await _status(store, fake_redis, "i1") == "answered"
    assert await _due_present(store, fake_redis, "i1")


# --- settle_visit_claim ----------------------------------------------------------------


def _answer(iid: str) -> InteractionResponse:
    return InteractionResponse(interaction_id=iid, answer="yes", answered_by="u-1", answered_at=datetime.now(UTC))


async def test_settle_reads_a_resume_open_until_it_is_answered(store, fake_redis) -> None:
    await _seed(store, fake_redis, "i1")
    await _seed(store, fake_redis, "i2")
    await _seed(store, fake_redis, "i3")
    act = VisitAct(action="resume", interaction_ids=("i1", "i2", "i3"), phase="before")
    assert await visit_module.settle_visit_claim(act) == ()

    # Answered through the answer door's claim, and one whose state is gone.
    assert await store.record_answer(
        fake_redis, _answer("i1"), "g1", 60, continuation_due_ttl=3600, continuation_first_attempt_at_ms=0
    )
    await fake_redis.delete(store.state_key("i3"))
    assert await visit_module.settle_visit_claim(act) == ("i1", "i3")


async def test_settle_reads_a_resume_answered_by_this_visit_as_gone(store, fake_redis) -> None:
    await _seed(store, fake_redis, "i1")
    async with _app():
        await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="i1", payload={"value": 1})], start=None, extras={}
        )
    act = VisitAct(action="resume", interaction_ids=("i1",), phase="committed")
    assert await visit_module.settle_visit_claim(act) == ("i1",)


async def test_settle_reads_a_take_open_while_its_outcome_waits(store, fake_redis) -> None:
    for cid in ("c1", "c2", "c3"):
        await _seed_outcome(store, fake_redis, cid)
    act = VisitAct(action="take", interaction_ids=("c1", "c2", "c3"), phase="before")
    assert await visit_module.settle_visit_claim(act) == ()

    with state_context(_context()):
        await visit_module.visit(target_name="t", cancel=[], resume=[TakeItem(id="c1")], start=None, extras={})
    await store.claim_outcome(fake_redis, "c2")
    assert await visit_module.settle_visit_claim(act) == ("c1", "c2")
    await store.delete_outcome(fake_redis, "c3")
    assert await visit_module.settle_visit_claim(act) == ("c1", "c2", "c3")


async def test_settle_raises_on_a_store_fault(store, fake_redis, monkeypatch) -> None:
    async def _down(self, r, iid):
        raise ConnectionError("store down")

    monkeypatch.setattr(InteractionStore, "get_state", _down)
    with pytest.raises(ConnectionError):
        await visit_module.settle_visit_claim(VisitAct(action="resume", interaction_ids=("i1",), phase="before"))


@pytest.mark.parametrize("action", ["cancel", "start"])
async def test_settle_refuses_an_act_that_is_no_claim(action) -> None:
    with pytest.raises(ValueError, match="only a resume or take claim"):
        await visit_module.settle_visit_claim(VisitAct(action=action, interaction_ids=(), phase="before"))
