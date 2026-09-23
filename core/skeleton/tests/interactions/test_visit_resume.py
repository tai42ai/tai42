"""The shared visit's INLINE resume drive — a REAL continuation tool through ``run_tool``.

Every resume drives the registered ``resume_tool`` face through the platform's ``run_tool`` and the
one delivery chokepoint (``drive_and_deliver``), never a fake handed the object directly. Covers:
the resume return shapes (final / re-park / ``ResumeBuffered``), the ``receives_outcome`` delivery
split (a live receiver takes the outcome inline vs a receiver-less resume subject-tracks it), the
two plain-raise arms, a ``ParkResumeFailed`` FAILED delivery, that a resume continuation
runs WITHOUT the door's state binding, and the undeclared-extras pre-check.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.interactions import (
    AnswerFormat,
    InteractionRequest,
    ResumeItem,
    UndeclaredExtrasKeyError,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.app import instance
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.runs.chokepoint import collect_resumed_interactions

_MOD = "tests.app._fixtures.resume_tool"
_AGENT_MOD = "tests.fixtures.extras_agent"


def _candidates() -> SubjectCandidates:
    return SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA"})


def _context() -> StateContext:
    return StateContext(door="conversation", candidates=_candidates(), actor="u-1", turn_id="t-1")


def _caller_park(
    store: InteractionStore,
    iid: str,
    *,
    gid: str = "g1",
    to: str = "caller",
    fmt: AnswerFormat = AnswerFormat.FREE,
    run_delivery_id: str | None = None,
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
        to=to,  # type: ignore[arg-type]
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=_context(),
        expiry_at=expiry,
        run_delivery_id=run_delivery_id,
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


@asynccontextmanager
async def _app_under(ctx: StateContext):
    """The tool app of :func:`_app` but with the ambient state context set to ``ctx``.

    Lets a resume run under a subject that differs from the one a park was stored under.
    """
    await _clear_server()
    async with instance.app.app_context(Manifest.model_validate({"tools": [{"title": "resume", "module": _MOD}]})):
        with state_context(ctx):
            yield


@asynccontextmanager
async def _agent_app():
    """A tool app that ALSO registers the ``extras_agent`` agent, for the agent-target extras check."""
    await _clear_server()
    manifest = Manifest.model_validate(
        {
            "tools": [{"title": "resume", "module": _MOD}],
            "agents": [{"title": "agents", "module": _AGENT_MOD, "include": ["extras_agent"]}],
        }
    )
    async with instance.app.app_context(manifest):
        with state_context(_context()):
            yield


async def _due_present(store: InteractionStore, fake_redis, iid: str) -> bool:
    raw = await fake_redis.hgetall(store.continuation_due_key(iid))
    return bool(raw)


# --- the resume return shapes (receives_outcome=True, a live inline receiver) -----------


async def test_inline_resume_returns_final(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="i1", payload={"value": 42})], start=None, extras={}
        )
    assert outcome.action == "resumed"
    assert outcome.kind == "result"
    assert outcome.result["value"] == 42
    assert outcome.result["status"] == "success"
    assert not await _due_present(store, fake_redis, "i1")  # due record cleared after a live-receiver terminal


async def test_inline_resume_reparks_user_only(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"kind": "repark_user", "new_id": "u9"})],
            start=None,
            extras={},
        )
    assert outcome.action == "resumed"
    assert outcome.kind == "parked"
    assert outcome.suspended is not None
    assert outcome.suspended.interaction_id == "u9"


async def test_inline_resume_returns_buffered_partitions_by_to(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    # The still-open siblings of the step: one caller ask, one user ask.
    await store.add(fake_redis, _caller_park(store, "r1", gid="g1"), idle_ttl=86400, to="caller")
    await store.add(
        fake_redis, _caller_park(store, "r2", gid="g1", to="user", fmt=AnswerFormat.TEXT), idle_ttl=86400, to="user"
    )
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"kind": "buffered", "remaining": ["r1", "r2"]})],
            start=None,
            extras={},
        )
    assert outcome.kind == "asks"
    assert [entry.id for entry in outcome.asks] == ["r1"]  # only the caller sibling surfaces


async def test_inline_resume_reparks_a_new_caller_ask(store, fake_redis):
    # The resumed run re-parks by asking the CALLER again: the visit surfaces the new caller ask as an
    # ``asks`` outcome (distinct from a ``ResumeBuffered`` partial and from a user-only ``parked``).
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    # The new caller ask the resumed run re-asks, parked on the same run subject.
    await store.add(fake_redis, _caller_park(store, "c2", gid="g1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"kind": "repark_caller", "new_id": "c2"})],
            start=None,
            extras={},
        )
    assert outcome.action == "resumed"
    assert outcome.kind == "asks"
    assert [entry.id for entry in outcome.asks] == ["c2"]
    assert outcome.asks[0].to == "caller"
    assert outcome.suspended is not None
    assert outcome.suspended.caller_interaction_ids == ["c2"]


async def test_inline_resume_repark_entries_read_where_the_resume_stored_them(store, fake_redis):
    # The park (i1) and the new caller ask it re-asks (c2) are indexed under subject A — their stored
    # ``continuation_state_context``, which the store derives the subject index from. The RESUMER
    # enters under a broader ambient subject B that includes A's key, so the resume's re-park is
    # stored on A's candidates while the visit normalises off B. The visit must still surface the
    # re-park's FULL entries — read where the resume stored them, not off the resumer's ambient
    # subject.
    b_candidates = SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA", "thread": "tB"})
    b_context = StateContext(door="conversation", candidates=b_candidates, actor="u-2", turn_id="t-2")
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    await store.add(fake_redis, _caller_park(store, "c2", gid="g1"), idle_ttl=86400, to="caller")
    async with _app_under(b_context):
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"kind": "repark_caller", "new_id": "c2"})],
            start=None,
            extras={},
        )
    assert outcome.kind == "asks"
    assert [entry.id for entry in outcome.asks] == ["c2"]
    assert outcome.asks[0].to == "caller"


async def test_cancel_one_and_resume_another_in_one_visit(store, fake_redis):
    # The README Part 5 "cancel + resume of another id" row: one visit tears down ``i1`` and drives
    # ``i2``'s continuation to a final — the cancel and the resume act on DIFFERENT ids of the run.
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    await store.add(fake_redis, _caller_park(store, "i2", gid="g1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=["i1"],
            resume=[ResumeItem(id="i2", payload={"value": 5})],
            start=None,
            extras={},
        )
    assert outcome.action == "resumed"
    assert outcome.cancelled == ["i1"]
    assert outcome.kind == "result"
    assert outcome.result["value"] == 5
    parked = {entry["id"] for entry in await store.list_parked_for(fake_redis, _candidates())}
    assert "i1" not in parked  # torn down
    assert "i2" not in parked  # resolved


async def test_resume_continuation_runs_without_the_door_binding(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t", cancel=[], resume=[ResumeItem(id="i1", payload={"value": 1})], start=None, extras={}
        )
    # The continuation dispatch carried NO door state binding (the binding is a START-only deposit).
    assert outcome.result["door_binding_present"] is False


# --- the resumed-interaction record note -----------------------------------------------


async def test_a_resumed_id_is_noted_on_the_ambient_record(store, fake_redis):
    # A successfully claimed and driven resume appends its interaction id to the ambient
    # run record's resumed-interaction collector.
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    async with _app():
        with collect_resumed_interactions() as resumed:
            await visit_module.visit(
                target_name="t", cancel=[], resume=[ResumeItem(id="i1", payload={"value": 1})], start=None, extras={}
            )
    assert resumed == ["i1"]


# --- the receives_outcome delivery split -----------------------------------------


async def test_receiverless_resume_subject_tracks_the_terminal(store, fake_redis):
    # A hook/schedule resume of a caller ask: no live receiver, so the chokepoint subject-tracks
    # the terminal (no address on the run) rather than returning it inline.
    await store.add(fake_redis, _caller_park(store, "i1", run_delivery_id="rid-1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"value": 7})],
            start=None,
            extras={},
            receives_outcome=False,
        )
    assert outcome.action == "resumed"
    assert outcome.kind == "none"  # nothing returned inline
    # The terminal waits on the subject under the run's completion id, for the next caller run to take.
    entries = await store.list_parked_for(fake_redis, _candidates())
    finished = [e for e in entries if e["status"] == "finished"]
    assert len(finished) == 1
    assert finished[0]["result"]["value"] == 7
    assert finished[0]["result"]["status"] == "success"


# --- the two plain-raise arms ---------------------------------------------------


async def test_plain_raise_with_receiver_clears_due_and_propagates(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1"), idle_ttl=86400, to="caller")
    async with _app():
        with pytest.raises(RuntimeError, match="resume drive blew up"):
            await visit_module.visit(
                target_name="t",
                cancel=[],
                resume=[ResumeItem(id="i1", payload={"kind": "boom"})],
                start=None,
                extras={},
            )
    assert not await _due_present(store, fake_redis, "i1")  # a live receiver owns the failure: due cleared


async def test_plain_raise_receiverless_keeps_due_for_the_reaper(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1", run_delivery_id="rid-1"), idle_ttl=86400, to="caller")
    async with _app():
        with pytest.raises(RuntimeError, match="resume drive blew up"):
            await visit_module.visit(
                target_name="t",
                cancel=[],
                resume=[ResumeItem(id="i1", payload={"kind": "boom"})],
                start=None,
                extras={},
                receives_outcome=False,
            )
    assert await _due_present(store, fake_redis, "i1")  # no receiver: the record is KEPT so the reaper redelivers


async def test_park_resume_failed_delivers_failed(store, fake_redis):
    await store.add(fake_redis, _caller_park(store, "i1", run_delivery_id="rid-1"), idle_ttl=86400, to="caller")
    async with _app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="i1", payload={"kind": "park_failed", "reason": "aborted"})],
            start=None,
            extras={},
            receives_outcome=False,
        )
    assert outcome.action == "resumed"
    # A failed terminal is subject-tracked FAILED and the due record is cleared (no redelivery).
    assert not await _due_present(store, fake_redis, "i1")
    entries = await store.list_parked_for(fake_redis, _candidates())
    failed = [e for e in entries if e["status"] == "failed"]
    assert len(failed) == 1


# --- undeclared extras ------------------------------------------------------------


async def test_undeclared_extras_key_raises_before_start(store):
    async def _start(extras):
        raise AssertionError("start must not run when an extras key is undeclared")

    async with _app():
        with pytest.raises(UndeclaredExtrasKeyError):
            await visit_module.visit(
                target_name="extras_target", cancel=[], resume=[], start=_start, extras={"undeclared": 1}
            )


async def test_declared_extras_key_is_accepted(store):
    async def _start(extras):
        return dict(extras)

    async with _app():
        outcome = await visit_module.visit(
            target_name="extras_target", cancel=[], resume=[], start=_start, extras={"declared": 5}
        )
    assert outcome.kind == "result"
    assert outcome.result == {"declared": 5}


async def test_agent_target_undeclared_extras_key_raises_before_start(store):
    # An AGENT target declares its keys through its ``extras_keys`` class attribute; an undeclared
    # key is refused by the same pre-check, before the start runs.
    async def _start(extras):
        raise AssertionError("start must not run when an agent extras key is undeclared")

    async with _agent_app():
        with pytest.raises(UndeclaredExtrasKeyError):
            await visit_module.visit(
                target_name="extras_agent", cancel=[], resume=[], start=_start, extras={"undeclared": 1}
            )


async def test_agent_target_declared_extras_key_is_accepted(store):
    # The key the agent declares through ``extras_keys`` passes the pre-check and the start runs.
    async def _start(extras):
        return dict(extras)

    async with _agent_app():
        outcome = await visit_module.visit(
            target_name="extras_agent", cancel=[], resume=[], start=_start, extras={"declared": 5}
        )
    assert outcome.kind == "result"
    assert outcome.result == {"declared": 5}
