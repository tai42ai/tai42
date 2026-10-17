"""The platform delivery chokepoint: one ladder delivers every resumed run's terminal.

A resumed run's continuation is driven through ``drive_and_deliver`` and the run's terminal is
delivered by the platform — fire the door's out-of-band address, else park a waiting outcome on the
run's subject, else drop — keyed by the run's per-run ``completion_id``. The continuation and the
address tool are BOTH reached by name through ``run_tool`` (never a hand-fed object), so the ladder
exercises the real name dispatch, the ``delivery_fire`` context, and the re-established run-delivery
identity.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    AnswerFormat,
    DeliveryAddress,
    InteractionRequest,
    InteractionResponse,
    ParkDeliveryUnauthorizedError,
    ParkResumeUnauthorizedError,
    ResumeBuffered,
    RunTerminalFailed,
    SuspendedInteraction,
)
from tai42_contract.states import SubjectCandidates
from tai42_contract.tools import RunDelivery, get_run_delivery, get_run_delivery_id, run_delivery

from tai42_skeleton.interactions import authorization, giveup_delivery
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions.store import ContinuationDue, serde
from tai42_skeleton.runs.chokepoint import delivery_fire, resume_origin

from ._continuation_support import configure_interactions_store, make_wired


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


class _Tools:
    """A ``run_tool`` fake that dispatches BY NAME to registered handlers.

    Records every dispatch with the ambient ``delivery_fire`` and ``run_delivery_id`` at call time,
    so a test can prove the continuation drove with the re-established identity and the address tool
    fired inside the platform's delivery-fire context.
    """

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, key, arguments, *, offload_sync=False, continues_chain=None):
        from tai42_skeleton.runs.chokepoint import get_delivery_fire

        self.calls.append(
            {
                "key": key,
                "arguments": dict(arguments),
                "delivery_fire": get_delivery_fire(),
                "run_delivery_id": get_run_delivery_id(),
                "run_delivery_address": get_run_delivery(),
                "continues_chain": continues_chain,
            }
        )
        handler = self.handlers.get(key)
        if handler is None:
            return None
        return handler(arguments)


@asynccontextmanager
async def _fake_bind(identity, *, bound_fingerprint=""):
    yield


def _wire_tools(monkeypatch, handlers: dict[str, Any]) -> _Tools:
    tools = _Tools(handlers)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools))
    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)
    return tools


async def _drive(
    wired,
    *,
    tool: str,
    interaction_id: str = "iid-1",
    delivery: DeliveryAddress | None,
    run_delivery_id: str | None = "rd-1",
    candidates: SubjectCandidates | None = None,
    receives_outcome: bool = False,
    ticket: bool = True,
) -> Any:
    # A receiver-less drive runs under the continuation-due ticket the answer claim wrote with it;
    # ``ticket=False`` models a ticket the kill seam revoked while the resume drove.
    if ticket and not receives_outcome:
        await _seed_due(wired, interaction_id)
    return await continuation_module.drive_and_deliver(
        wired.store,
        identity="svc-key",
        fingerprint="fp-1",
        tool=tool,
        interaction_id=interaction_id,
        answer={"a": 1},
        park_context=None,
        park_asked_by=("main",),
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=candidates,
        receives_outcome=receives_outcome,
    )


def _cid(run_delivery_id: str) -> str:
    return continuation_module._completion_id(run_delivery_id)


async def _seed_due(wired, interaction_id: str) -> None:
    # A durable continuation-due record the drive clears on a healthy delivery.
    await wired.fake.hset(
        wired.store.continuation_due_key(interaction_id),
        mapping={
            "tool": "resume_tool",
            "identity": "svc-key",
            "fingerprint": "fp-1",
            "answer": "null",
            "attempts": "0",
        },
    )
    await wired.fake.zadd(wired.store.continuation_due_index_key, {interaction_id: 0})


async def _due_present(wired, interaction_id: str) -> bool:
    raw = await wired.fake.hgetall(wired.store.continuation_due_key(interaction_id))
    return bool(raw)


# --- the ladder's terminal-fire rung ----------------------------------------------------


async def test_terminal_success_fires_the_address_with_the_run_completion_id(wired, monkeypatch):
    tools = _wire_tools(
        monkeypatch,
        {
            "resume_tool": lambda _a: {"status": "success", "value": 7},
            "deliver_tool": lambda a: {"message_id": a["completion_id"]},
        },
    )
    await _seed_due(wired, "iid-1")
    await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )

    # The continuation drove with the RE-ESTABLISHED run-delivery identity and NO delivery-fire,
    # then the address fired ONCE, inside the fire context, carrying the run's completion id + result.
    drive_call = next(c for c in tools.calls if c["key"] == "resume_tool")
    assert drive_call["run_delivery_id"] == "rd-1"
    assert drive_call["run_delivery_address"] == DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1")
    assert drive_call["delivery_fire"] is None
    fire = next(c for c in tools.calls if c["key"] == "deliver_tool")
    assert fire["arguments"] == {
        "thread_id": "t1",
        "result": {"status": "success", "value": 7},
        "completion_id": _cid("rd-1"),
        "status": PARK_COMPLETION_SUCCEEDED,
    }
    assert fire["delivery_fire"] == _cid("rd-1")
    # The due record is cleared only after the terminal is delivered.
    assert not await _due_present(wired, "iid-1")


async def test_a_returned_value_is_fired_succeeded_without_status_inspection(wired, monkeypatch):
    # A RETURN from a resume is a SUCCESS (a failure RAISES RunTerminalFailed): the delivery fires
    # SUCCEEDED with no inspection of the returned value, so a success payload that happens to carry
    # a ``status`` key reading failure-adjacent is delivered as the success it is.
    tools = _wire_tools(
        monkeypatch,
        {"resume_tool": lambda _a: {"status": "failed", "error": "a plain field"}, "deliver_tool": lambda a: None},
    )
    await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )
    fire = next(c for c in tools.calls if c["key"] == "deliver_tool")
    assert fire["arguments"]["status"] == PARK_COMPLETION_SUCCEEDED
    assert fire["arguments"]["result"] == {"status": "failed", "error": "a plain field"}


async def test_run_terminal_failed_delivers_failed_and_clears_due(wired, monkeypatch):
    def _raise(_a):
        raise RunTerminalFailed({"aborted": True})

    tools = _wire_tools(monkeypatch, {"resume_tool": _raise, "deliver_tool": lambda a: None})
    await _seed_due(wired, "iid-1")
    await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )
    fire = next(c for c in tools.calls if c["key"] == "deliver_tool")
    assert fire["arguments"]["status"] == PARK_COMPLETION_FAILED
    assert fire["arguments"]["result"] == {"aborted": True}
    assert not await _due_present(wired, "iid-1")


async def test_plain_raise_keeps_the_due_record_and_fires_nothing(wired, monkeypatch):
    def _raise(_a):
        raise RuntimeError("worker crashed mid-resume")

    tools = _wire_tools(monkeypatch, {"resume_tool": _raise, "deliver_tool": lambda a: None})
    await _seed_due(wired, "iid-1")
    with pytest.raises(RuntimeError):
        await _drive(
            wired,
            tool="resume_tool",
            delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
            run_delivery_id="rd-1",
        )
    # A receiver-less plain raise KEEPS the record for the reaper and fires no address.
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    assert await _due_present(wired, "iid-1")


async def test_address_delivery_drops_the_subject_index_membership(wired, monkeypatch):
    # An answered park moves ``asking`` → ``running`` and KEEPS its subject-index membership while
    # the run drives. When the terminal is delivered through an ADDRESS the run is complete, so that
    # running membership must leave — exactly as the subject-track rung drops it via ``add_outcome``.
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success"}, "deliver_tool": lambda a: None})
    candidates = SubjectCandidates(target_kind="tool", target_name="tool-a", by_kind={"person": "p1"})
    parks_key = wired.store.subject_parks_key("tool", "tool-a", "person", "p1")
    await wired.fake.sadd(parks_key, "iid-1")

    await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
        candidates=candidates,
    )

    # The address fired AND the interaction left the subject index (its run is complete).
    assert any(c["key"] == "deliver_tool" for c in tools.calls)
    assert await wired.fake.smembers(parks_key) == set()


# --- the ladder's subject and drop rungs ------------------------------------------------


async def test_no_address_with_subject_writes_a_waiting_outcome(wired, monkeypatch):
    _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success", "value": 5}})
    candidates = SubjectCandidates(target_kind="tool", target_name="tool-a", by_kind={"person": "p1"})
    await _drive(wired, tool="resume_tool", delivery=None, run_delivery_id="rd-1", candidates=candidates)
    outcome = await wired.store.claim_outcome(wired.fake, _cid("rd-1"))
    assert outcome is not None
    assert outcome.status == "finished"
    assert outcome.result == {"status": "success", "value": 5}


async def test_no_address_no_subject_drops_the_outcome(wired, monkeypatch, caplog):
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success"}})
    caplog.set_level(logging.INFO)
    await _drive(wired, tool="resume_tool", delivery=None, run_delivery_id="rd-1", candidates=None)
    assert not any(c["key"] != "resume_tool" for c in tools.calls)  # nothing fired
    assert any("no delivery address and no subject" in rec.message for rec in caplog.records)


async def test_withdrawn_terminal_with_a_subject_writes_no_outcome_and_leaves_the_index(wired, monkeypatch):
    # A run the platform took down on purpose has no outcome for anyone to take: the subject rung
    # writes nothing and the entry leaves its subject index.
    from tai42_contract.interactions import PARK_COMPLETION_WITHDRAWN

    tools = _wire_tools(monkeypatch, {})
    candidates = SubjectCandidates(target_kind="tool", target_name="tool-a", by_kind={"person": "p1"})
    parks_key = wired.store.subject_parks_key("tool", "tool-a", "person", "p1")
    await wired.fake.sadd(parks_key, "iid-w")

    await continuation_module._deliver_terminal(
        wired.store,
        interaction_id="iid-w",
        outcome={"tai42:run_killed": "cancelled"},
        status=PARK_COMPLETION_WITHDRAWN,
        delivery=None,
        run_delivery_id="rd-w",
        candidates=candidates,
    )

    assert await wired.store.claim_outcome(wired.fake, _cid("rd-w")) is None
    assert await wired.fake.smembers(parks_key) == set()
    assert tools.calls == []


async def test_subject_waiting_outcome_is_written_once_on_redelivery(wired, monkeypatch):
    _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success", "value": 9}})
    candidates = SubjectCandidates(target_kind="tool", target_name="tool-a", by_kind={"person": "p1"})
    # Two drives of the SAME run terminal (a redelivery): the waiting outcome is keyed by the run's
    # completion id, so the second write no-ops — exactly one row.
    await _drive(wired, tool="resume_tool", delivery=None, run_delivery_id="rd-1", candidates=candidates)
    await _drive(wired, tool="resume_tool", delivery=None, run_delivery_id="rd-1", candidates=candidates)
    assert await wired.store.claim_outcome(wired.fake, _cid("rd-1")) is not None
    assert await wired.store.claim_outcome(wired.fake, _cid("rd-1")) is None  # only one row existed


async def test_redelivery_fires_the_same_completion_id(wired, monkeypatch):
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success"}, "deliver_tool": lambda a: None})
    await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )
    await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )
    fires = [c for c in tools.calls if c["key"] == "deliver_tool"]
    assert len(fires) == 2
    # Both fires carry the SAME per-run completion id, so completion_delivery dedups the second.
    assert fires[0]["arguments"]["completion_id"] == fires[1]["arguments"]["completion_id"] == _cid("rd-1")


# --- non-terminal returns stay parked ----------------------------------------------------


async def test_reparked_suspended_interaction_delivers_nothing(wired, monkeypatch):
    tools = _wire_tools(
        monkeypatch,
        {"resume_tool": lambda _a: SuspendedInteraction(interaction_id="iid-2"), "deliver_tool": lambda a: None},
    )
    await _seed_due(wired, "iid-1")
    result = await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )
    assert isinstance(result, SuspendedInteraction)
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    assert not await _due_present(
        wired, "iid-1"
    )  # the old due record is cleared; the new park carries delivery forward


async def test_resume_buffered_delivers_nothing(wired, monkeypatch):
    tools = _wire_tools(
        monkeypatch,
        {"resume_tool": lambda _a: ResumeBuffered(remaining_ids=["iid-3"]), "deliver_tool": lambda a: None},
    )
    result = await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
    )
    assert isinstance(result, ResumeBuffered)
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)


# --- a receiver-less terminal is delivered only while the resume's ticket stands ----------


async def test_receiver_less_terminal_is_not_delivered_once_its_ticket_is_gone(wired, monkeypatch, caplog):
    tools = _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success"}, "deliver_tool": lambda a: None})
    address = DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1")
    # With the ticket standing the terminal is delivered and the ticket cleared.
    await _seed_due(wired, "iid-t")
    await _drive(wired, tool="resume_tool", interaction_id="iid-t", delivery=address, run_delivery_id="rd-t")
    assert [c["key"] for c in tools.calls].count("deliver_tool") == 1
    assert not await _due_present(wired, "iid-t")

    # The kill walk revoked the ticket while the resume drove: nothing is delivered, nothing is parked.
    caplog.set_level(logging.INFO)
    candidates = SubjectCandidates(target_kind="tool", target_name="tool-a", by_kind={"person": "p9"})
    await _drive(
        wired, tool="resume_tool", interaction_id="iid-g", delivery=address, run_delivery_id="rd-g", ticket=False
    )
    await _drive(
        wired,
        tool="resume_tool",
        interaction_id="iid-h",
        delivery=None,
        run_delivery_id="rd-h",
        candidates=candidates,
        ticket=False,
    )
    assert [c["key"] for c in tools.calls].count("deliver_tool") == 1
    assert await wired.store.claim_outcome(wired.fake, _cid("rd-h")) is None
    assert any("iid-g" in rec.getMessage() and "nothing is delivered" in rec.getMessage() for rec in caplog.records)


async def test_receiver_less_failed_terminal_is_not_delivered_once_its_ticket_is_gone(wired, monkeypatch, caplog):
    def _raise(_a):
        raise RunTerminalFailed({"status": "aborted"})

    tools = _wire_tools(monkeypatch, {"resume_tool": _raise, "deliver_tool": lambda a: None})
    caplog.set_level(logging.INFO)
    await _drive(
        wired,
        tool="resume_tool",
        interaction_id="iid-f",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-f",
        ticket=False,
    )
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    assert any("iid-f" in rec.getMessage() and "nothing is delivered" in rec.getMessage() for rec in caplog.records)


async def test_inline_resume_returns_its_outcome_regardless_of_the_ticket(wired, monkeypatch):
    _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success", "value": 7}})
    result = await _drive(
        wired,
        tool="resume_tool",
        interaction_id="iid-i",
        delivery=None,
        run_delivery_id="rd-i",
        receives_outcome=True,
    )
    assert result == {"status": "success", "value": 7}


# --- the inline-receiver rung ------------------------------------------------------------


async def test_inline_receiver_gets_the_outcome_and_nothing_is_fired(wired, monkeypatch):
    tools = _wire_tools(
        monkeypatch,
        {"resume_tool": lambda _a: {"status": "success", "value": 3}, "deliver_tool": lambda a: None},
    )
    result = await _drive(
        wired,
        tool="resume_tool",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
        run_delivery_id="rd-1",
        receives_outcome=True,
    )
    # A live inline receiver takes the terminal; no address is fired and the resumer's own ambient
    # run-delivery context is left in place (the drive did NOT re-establish the stored one).
    assert result == {"status": "success", "value": 3}
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    drive_call = next(c for c in tools.calls if c["key"] == "resume_tool")
    assert drive_call["run_delivery_id"] is None


async def test_inline_receiver_propagates_a_run_terminal_failed(wired, monkeypatch):
    def _raise(_a):
        raise RunTerminalFailed({"aborted": True})

    tools = _wire_tools(monkeypatch, {"resume_tool": _raise, "deliver_tool": lambda a: None})
    with pytest.raises(RunTerminalFailed):
        await _drive(
            wired,
            tool="resume_tool",
            delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1"),
            run_delivery_id="rd-1",
            receives_outcome=True,
        )
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)


# --- the missing-id loud raise -----------------------------------------------------------


async def test_terminal_with_no_run_delivery_id_raises_loudly(wired, monkeypatch):
    _wire_tools(monkeypatch, {"resume_tool": lambda _a: {"status": "success"}})
    with pytest.raises(RuntimeError, match="no stored run_delivery_id"):
        await _drive(wired, tool="resume_tool", delivery=None, run_delivery_id=None, candidates=None)


# --- the authorization facets ------------------------------------------------------------


async def test_assert_delivery_authorized_only_inside_the_matching_fire():
    cid = "c-1"
    # Inside the fire for the same id: passes.
    with delivery_fire(cid):
        authorization.assert_delivery_authorized(cid)
    # Outside any fire, inside a fire for another id, and for a None id: refused.
    with pytest.raises(ParkDeliveryUnauthorizedError):
        authorization.assert_delivery_authorized(cid)
    with delivery_fire("other"), pytest.raises(ParkDeliveryUnauthorizedError):
        authorization.assert_delivery_authorized(cid)
    with delivery_fire(cid), pytest.raises(ParkDeliveryUnauthorizedError):
        authorization.assert_delivery_authorized(None)


async def test_assert_resume_authorized_by_origin_and_by_run_identity(wired, monkeypatch):
    # Point the authorization store read at the same fake redis + settings the wiring uses.
    monkeypatch.setattr(authorization, "client_ctx", monkeypatch_client_ctx(wired))
    monkeypatch.setattr(authorization, "interactions_settings", lambda: wired.settings)
    monkeypatch.setattr(authorization, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(authorization, "InteractionStore", lambda _prefix: wired.store)

    # A stored park of run ``rd-9`` under interaction ``iid-9``.
    await wired.store.add(
        wired.fake,
        InteractionRequest(
            interaction_id="iid-9",
            group_id="g9",
            question="?",
            answer_format=AnswerFormat.TEXT,
            reply_to=wired.store.reply_key("iid-9"),
            created_at=datetime.now(UTC),
            timeout_at=datetime.now(UTC) + timedelta(hours=1),
            mode="async",
            continuation_tool="resume_tool",
            continuation_identity="svc-key",
            expiry_at=datetime.now(UTC) + timedelta(hours=1),
            run_delivery_id="rd-9",
        ),
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        run_delivery_id="rd-9",
    )

    # Authorised when the resume origin IS the interaction (no store read needed).
    with resume_origin("iid-9"):
        await authorization.assert_resume_authorized("iid-9")

    # Authorised for a DIFFERENT interaction of the SAME run (a cross-driver chain re-entry): the
    # origin differs but the ambient run identity equals the interaction's stored one.
    with resume_origin("other-iid"), run_delivery(RunDelivery("rd-9", None)):
        await authorization.assert_resume_authorized("iid-9")

    # Refused with no origin (an external run-tool / MCP caller).
    with pytest.raises(ParkResumeUnauthorizedError):
        await authorization.assert_resume_authorized("iid-9")

    # Refused when the ambient run identity belongs to ANOTHER run (a leaked origin from a deeper run).
    with (
        resume_origin("other-iid"),
        run_delivery(RunDelivery("rd-other", None)),
        pytest.raises(ParkResumeUnauthorizedError),
    ):
        await authorization.assert_resume_authorized("iid-9")


async def test_a_face_entry_for_a_taken_down_run_is_refused(wired, monkeypatch):
    from tai42_contract.interactions import ParkRunTakenDownError

    from tai42_skeleton.runs.chokepoint import resume_lineage

    monkeypatch.setattr(authorization, "client_ctx", monkeypatch_client_ctx(wired))
    monkeypatch.setattr(authorization, "interactions_settings", lambda: wired.settings)
    monkeypatch.setattr(authorization, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(authorization, "InteractionStore", lambda _prefix: wired.store)
    chain_key = "tai42:chained-park:kd"
    # The run was taken down: its kill mark stands.
    await wired.fake.set(wired.store.run_killed_key("rd-dead"), "cancelled")

    # The drive of the interaction itself, a sibling of its run and a chained call of its lineage are
    # all refused while the ambient run is dead.
    with resume_origin("iid-d"), run_delivery(RunDelivery("rd-dead", None)), pytest.raises(ParkRunTakenDownError):
        await authorization.assert_resume_authorized("iid-d")
    with (
        resume_origin("iid-d"),
        resume_lineage([chain_key]),
        run_delivery(RunDelivery("rd-dead", None)),
        pytest.raises(ParkRunTakenDownError),
    ):
        await authorization.assert_resume_authorized(chain_key)

    # A live run is authorised as before.
    with resume_origin("iid-l"), run_delivery(RunDelivery("rd-live", None)):
        await authorization.assert_resume_authorized("iid-l")
    with resume_origin("iid-l"), resume_lineage([chain_key]), run_delivery(RunDelivery("rd-live", None)):
        await authorization.assert_resume_authorized(chain_key)


async def test_a_receiver_less_drive_of_a_taken_down_run_delivers_nothing_at_info(wired, monkeypatch, caplog):
    from tai42_contract.interactions import ParkRunTakenDownError

    def _taken_down(_a):
        raise ParkRunTakenDownError("resume of 'iid-x' refused: its run was taken down")

    tools = _wire_tools(monkeypatch, {"resume_tool": _taken_down, "deliver_tool": lambda a: None})
    caplog.set_level(logging.INFO)
    address = DeliveryAddress(tool="deliver_tool", context=None, thread_id="t1")

    assert (
        await _drive(wired, tool="resume_tool", interaction_id="iid-x", delivery=address, run_delivery_id="rd-x")
        is None
    )

    assert not any(c["key"] == "deliver_tool" for c in tools.calls)
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    infos = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.INFO and "iid-x" in rec.getMessage() and "taken down" in rec.getMessage()
    ]
    assert len(infos) == 1

    # The inline receiver is told: the due record is cleared and the refusal re-raised.
    await _seed_due(wired, "iid-y")
    with pytest.raises(ParkRunTakenDownError):
        await _drive(
            wired,
            tool="resume_tool",
            interaction_id="iid-y",
            delivery=address,
            run_delivery_id="rd-y",
            receives_outcome=True,
        )
    assert not await _due_present(wired, "iid-y")
    assert not any(c["key"] == "deliver_tool" for c in tools.calls)


def monkeypatch_client_ctx(wired):
    @asynccontextmanager
    async def _ctx(_cls, _settings):
        yield wired.fake

    return _ctx


async def test_redelivery_horizon_is_the_idle_ttl(wired):
    assert authorization.redelivery_horizon_seconds() == wired.settings.idle_ttl_seconds


# --- the permanent give-up ---------------------------------------------------------------


async def test_permanent_giveup_delivers_failed_off_the_stored_delivery(wired, monkeypatch):
    tools = _wire_tools(monkeypatch, {"deliver_tool": lambda a: None})
    request = InteractionRequest(
        interaction_id="iid-g",
        group_id="gg",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=wired.store.reply_key("iid-g"),
        created_at=datetime.now(UTC),
        timeout_at=datetime.now(UTC) + timedelta(hours=1),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
        run_delivery_id="rd-g",
        delivery=DeliveryAddress(tool="deliver_tool", context=None, thread_id="tg"),
    )
    await giveup_delivery.deliver_park_giveup(wired.store, request, fingerprint=None)
    fire = next(c for c in tools.calls if c["key"] == "deliver_tool")
    assert fire["arguments"]["status"] == PARK_COMPLETION_FAILED
    assert fire["arguments"]["completion_id"] == _cid("rd-g")
    assert fire["delivery_fire"] == _cid("rd-g")


# --- persist denormalizes the run address; the reaper fast-path reads it back --------------


def _caller_ask(store, *, iid: str, run_delivery_id: str, delivery: DeliveryAddress):
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id="gp",
        question="?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=now + timedelta(hours=1),
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        expiry_at=now + timedelta(hours=1),
        run_delivery_id=run_delivery_id,
        delivery=delivery,
    )


async def test_persist_denormalizes_delivery_and_the_redelivery_fastpath_reads_it(wired):
    # persist passes the run's address and its ``run_delivery_id`` into ``store.add``; the state
    # hash carries both, and the answer path copies them onto the continuation-due record so the
    # reaper's DETACHED redelivery reconstructs the run's address + delivery identity without
    # re-reading the request.
    address = DeliveryAddress(tool="deliver_tool", context={"route_name": "r"}, thread_id="tp")
    request = _caller_ask(wired.store, iid="iid-p", run_delivery_id="rd-p", delivery=address)
    await wired.store.add(
        wired.fake,
        request,
        idle_ttl=86400,
        continuation_fingerprint="fp-1",
        to="caller",
        delivery=address,
        run_delivery_id="rd-p",
    )

    # The durable state hash carries the denormalized address + identity.
    raw = await wired.fake.hgetall(wired.store.state_key("iid-p"))
    fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
    assert DeliveryAddress.model_validate_json(fields["delivery"]) == address
    assert fields["run_delivery_id"] == "rd-p"

    # Resolve the ask, then the reaper's retry-claim reconstructs a due record carrying both.
    now = datetime.now(UTC)
    due_ttl, first_attempt = continuation_module.continuation_due_timing(wired.settings)
    claimed = await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="iid-p", answer="go", answered_by="u1", answered_at=now),
        "gp",
        60,
        continuation_due_ttl=due_ttl,
        continuation_first_attempt_at_ms=first_attempt,
    )
    assert claimed is True
    due = await wired.store.claim_continuation_retry(wired.fake, "iid-p", now + timedelta(hours=2), 1000, 1000)
    assert isinstance(due, ContinuationDue)
    assert due.delivery == address
    assert due.run_delivery_id == "rd-p"


async def test_a_stored_address_lacking_its_thread_fails_to_decode_loudly(wired):
    # Every address field is required: a stored address of any other shape never decodes into a
    # fire, so no delivery tool is reached with a guessed thread.
    await _seed_due(wired, "iid-old")
    await wired.fake.hset(
        wired.store.continuation_due_key("iid-old"),
        mapping={"delivery": '{"tool": "deliver_tool", "context": {"route_name": "r"}}', "run_delivery_id": "rd-o"},
    )

    with pytest.raises(ValidationError, match="thread_id"):
        await wired.store.claim_continuation_retry(
            wired.fake, "iid-old", datetime.now(UTC) + timedelta(hours=2), 1000, 1000
        )
