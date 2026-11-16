"""What a continuation drive tells the working signal of the run it resumes.

A receiver-less drive of a run addressed to a thread holds the run's signal for its whole body and
first states the answer it resumes on, with the run's questions still open; a drive with a live
receiver states the answer only to a signal running for the receiver's run. The answering message's
medium id rides every resume door to the drive.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.interactions import (
    DeliveryAddress,
    InteractionResponse,
    ResumeBuffered,
    reset_park_completion,
    set_park_completion,
)

from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.turn.completion_delivery import COMPLETION_TOOL_NAME
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import continuation_dispatch as continuation_dispatch_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions.run_completion import run_completion_id
from tai42_skeleton.interactions.store import ContinuationDue, InteractionStore

from ._continuation_support import async_req, configure_interactions_store, drain, make_wired

RUN_DELIVERY = "rd-ws"
THREAD = "bridge:line:+15550002222"
ADDRESS = DeliveryAddress(tool="probe_deliver", context={"route_name": "line"}, thread_id=THREAD)
# A drive that left a sibling open: non-terminal, so nothing is delivered.
_BUFFERED = ResumeBuffered(remaining_ids=["q2"])


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


class _Signal:
    """Records the drive's calls on the working signal; ``loops`` are the run ids with a live loop."""

    def __init__(self, *loops: str) -> None:
        self.loops = set(loops)
        self.events: list[tuple[Any, ...]] = []

    @contextlib.contextmanager
    def hold(self, run_id: str, *, thread_id: str):
        self.events.append(("hold", run_id, thread_id))
        try:
            yield
        finally:
            self.events.append(("release", run_id))

    def has_loop(self, run_id: str | None) -> bool:
        return run_id in self.loops

    def resumed(self, run_id: str, interaction_id: str, *, medium_message_id: str | None, pending) -> None:
        self.events.append(("resumed", run_id, interaction_id, medium_message_id, list(pending)))


@pytest.fixture
def signal(monkeypatch) -> _Signal:
    fake = _Signal(run_completion_id(RUN_DELIVERY))
    monkeypatch.setattr(working_signal, "hold", fake.hold)
    monkeypatch.setattr(working_signal, "has_loop", fake.has_loop)
    monkeypatch.setattr(working_signal, "resumed", fake.resumed)
    return fake


def _continuation(signal: _Signal, outcome: Any = None, *, raises: Exception | None = None):
    async def _run(identity, fingerprint, tool, interaction_id, answer, *args, **kwargs):
        signal.events.append(("drive", interaction_id))
        if raises is not None:
            raise raises
        return outcome

    return _run


def _ladder(signal: _Signal):
    async def _deliver(store, *, interaction_id, **kwargs):
        signal.events.append(("deliver", interaction_id))

    return _deliver


async def _park(wired, iid: str) -> None:
    request = async_req(wired.store, iid=iid).model_copy(update={"run_delivery_id": RUN_DELIVERY})
    await wired.store.add(wired.fake, request, idle_ttl=86400, run_delivery_id=RUN_DELIVERY)


async def _drive(
    wired,
    *,
    interaction_id: str = "q1",
    medium_message_id: str | None = "M-1",
    delivery: DeliveryAddress | None = ADDRESS,
    run_delivery_id: str | None = RUN_DELIVERY,
    receives_outcome: bool = False,
) -> Any:
    return await continuation_module.drive_and_deliver(
        wired.store,
        identity="svc-key",
        fingerprint="fp-1",
        tool="resume_tool",
        interaction_id=interaction_id,
        answer="yes",
        reply_to_medium_message_id=None,
        medium_message_id=medium_message_id,
        params=None,
        park_context=None,
        park_asked_by=("main",),
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=None,
        receives_outcome=receives_outcome,
    )


# --- the store reads the run's open questions -------------------------------------------------


async def test_the_runs_open_questions_are_its_pending_parks_but_the_named_one(wired):
    for iid in ("q1", "q2", "q3"):
        await _park(wired, iid)
    now = datetime.now(UTC)
    assert await wired.store.record_answer(
        wired.fake,
        InteractionResponse(interaction_id="q2", answer="y", answered_by="op", answered_at=now),
        "ag",
        60,
        continuation_due_ttl=3600,
        continuation_first_attempt_at_ms=int(now.timestamp() * 1000) + 10_000,
    )

    assert await wired.store.run_members(wired.fake, RUN_DELIVERY) == ["q1", "q2", "q3"]
    assert await wired.store.pending_run_members(wired.fake, RUN_DELIVERY, excluding="q1") == ["q3"]
    assert await InteractionStore("other:").pending_run_members(wired.fake, RUN_DELIVERY, excluding="q1") == []


# --- the answering message's medium id rides every resume door ------------------------------


def _response(iid: str, medium_message_id: str | None) -> InteractionResponse:
    return InteractionResponse(
        interaction_id=iid,
        answer="yes",
        answered_by="tester",
        answered_at=datetime.now(UTC),
        medium_message_id=medium_message_id,
    )


async def test_the_due_record_carries_the_answering_messages_medium_id(wired):
    await wired.store.add(wired.fake, async_req(wired.store, iid="a1"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await wired.store.record_answer(
        wired.fake, _response("a1", "M-2"), "ag", 60, continuation_due_ttl=86400, continuation_first_attempt_at_ms=0
    )
    raw = await wired.fake.hgetall(wired.store.continuation_due_key("a1"))
    assert raw["medium_message_id"] == "M-2"
    due = await wired.store.claim_continuation_retry(wired.fake, "a1", datetime.now(UTC), 1000, 10000)
    assert isinstance(due, ContinuationDue)
    assert due.medium_message_id == "M-2"


async def test_a_due_record_of_an_answer_from_no_message_carries_no_medium_id(wired):
    await wired.store.add(wired.fake, async_req(wired.store, iid="a2"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await wired.store.record_answer(
        wired.fake, _response("a2", None), "ag", 60, continuation_due_ttl=86400, continuation_first_attempt_at_ms=0
    )
    assert "medium_message_id" not in await wired.fake.hgetall(wired.store.continuation_due_key("a2"))
    due = await wired.store.claim_continuation_retry(wired.fake, "a2", datetime.now(UTC), 1000, 10000)
    assert isinstance(due, ContinuationDue)
    assert due.medium_message_id is None


@pytest.fixture
def driven(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _drive_and_deliver(store, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(continuation_dispatch_module, "drive_and_deliver", _drive_and_deliver)
    return calls


async def test_a_claimed_answer_hands_its_medium_id_to_the_drive(wired, driven):
    continuation_dispatch_module.dispatch_continuation(
        wired.store, async_req(wired.store, iid="d1"), "fp", _response("d1", "M-3")
    )
    await drain()
    assert [c["medium_message_id"] for c in driven] == ["M-3"]


async def test_a_redelivery_hands_the_due_records_medium_id_to_the_drive(wired, driven):
    await wired.store.add(wired.fake, async_req(wired.store, iid="d2"), idle_ttl=86400, continuation_fingerprint="fp")
    assert await wired.store.record_answer(
        wired.fake, _response("d2", "M-4"), "ag", 60, continuation_due_ttl=86400, continuation_first_attempt_at_ms=0
    )
    assert await reaper_module.redeliver_due_continuations_once() == 1
    await drain()
    assert [c["medium_message_id"] for c in driven] == ["M-4"]


# --- a receiver-less drive holds the run's signal ----------------------------------------------


async def test_a_receiverless_drive_holds_the_signal_over_the_drive_and_the_delivery(wired, signal, monkeypatch):
    await _park(wired, "q1")
    await _park(wired, "q2")
    monkeypatch.setattr(continuation_module, "_run_continuation", _continuation(signal, {"ok": True}))
    monkeypatch.setattr(continuation_module, "_deliver_receiverless_outcome", _ladder(signal))

    assert await _drive(wired) is None

    run_id = run_completion_id(RUN_DELIVERY)
    assert signal.events == [
        ("hold", run_id, THREAD),
        ("resumed", run_id, "q1", "M-1", ["q2"]),
        ("drive", "q1"),
        ("deliver", "q1"),
        ("release", run_id),
    ]


async def test_a_receiverless_drive_that_raises_releases_the_signal_and_keeps_its_due_record(
    wired, signal, monkeypatch
):
    await wired.fake.hset(
        wired.store.continuation_due_key("q1"),
        mapping={
            "tool": "resume_tool",
            "identity": "svc-key",
            "fingerprint": "fp-1",
            "answer": "null",
            "attempts": "0",
        },
    )
    monkeypatch.setattr(continuation_module, "_run_continuation", _continuation(signal, raises=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await _drive(wired)

    assert [e[0] for e in signal.events] == ["hold", "resumed", "drive", "release"]
    assert await wired.fake.hgetall(wired.store.continuation_due_key("q1")) != {}


@pytest.mark.parametrize(
    ("delivery", "run_delivery_id"),
    [(DeliveryAddress(tool="probe_deliver", context=None, thread_id=None), RUN_DELIVERY), (ADDRESS, None)],
    ids=["no-thread", "no-run-delivery"],
)
async def test_a_drive_of_a_run_with_no_thread_or_no_delivery_identity_holds_nothing(
    wired, signal, monkeypatch, delivery, run_delivery_id
):
    monkeypatch.setattr(continuation_module, "_run_continuation", _continuation(signal, _BUFFERED))
    await _drive(wired, delivery=delivery, run_delivery_id=run_delivery_id)
    assert [e[0] for e in signal.events if e[0] in ("hold", "release")] == []


async def test_a_failed_read_of_the_runs_open_questions_is_logged_and_the_drive_goes_on(
    wired, signal, monkeypatch, caplog
):
    async def _fails(r, run_delivery_id, *, excluding):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(wired.store, "pending_run_members", _fails)
    monkeypatch.setattr(continuation_module, "_run_continuation", _continuation(signal, _BUFFERED))

    with caplog.at_level(logging.ERROR, logger=continuation_module.__name__):
        result = await _drive(wired)

    assert isinstance(result, ResumeBuffered)
    assert [e[0] for e in signal.events] == ["hold", "drive", "release"]
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


# --- a drive with a live receiver states the answer to the receiver's run ----------------------


@contextlib.contextmanager
def _turn(message_id: str):
    token = set_park_completion(COMPLETION_TOOL_NAME, {"message_id": message_id}, thread_id=THREAD)
    try:
        yield
    finally:
        reset_park_completion(token)


async def test_a_receiver_drive_with_no_signal_for_its_run_reads_nothing(wired, signal, monkeypatch):
    reads: list[str] = []

    async def _spy(r, run_delivery_id, *, excluding):
        reads.append(excluding)
        return []

    monkeypatch.setattr(wired.store, "pending_run_members", _spy)
    monkeypatch.setattr(continuation_module, "_run_continuation", _continuation(signal, _BUFFERED))

    with _turn("turn-without-loop"):
        await _drive(wired, receives_outcome=True)

    assert reads == []
    assert [e[0] for e in signal.events] == ["drive"]


async def test_a_receiver_drive_states_the_answer_to_its_turns_signal_lending_no_medium_id(wired, signal, monkeypatch):
    await _park(wired, "q1")
    await _park(wired, "q2")
    signal.loops.add("turn-1")
    monkeypatch.setattr(continuation_module, "_run_continuation", _continuation(signal, _BUFFERED))

    with _turn("turn-1"):
        await _drive(wired, receives_outcome=True)

    assert signal.events == [("resumed", "turn-1", "q1", None, ["q2"]), ("drive", "q1")]
