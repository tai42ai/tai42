"""What an ask tells the working signal of the run asking.

Every question the run then waits on pauses the signal once it is persisted, however it is
delivered and whoever answers it. The ask that sent it closes it: answered, with the answering
message's id, when it returns the answer; withdrawn when it raises (a refused delivery, a failed or
timed-out wait, a failed read of the answer's quote, a cancel).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelAddress, ChannelDelivery, SendOutcome
from tai42_contract.interactions import (
    InteractionResponse,
    SuspendedInteraction,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
)
from tai42_contract.tools import RunDelivery, run_delivery, tool_call_frame
from tests._helpers import DeliverOnlyChannel, await_add_event

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.conversations import working_signal
from tai42_skeleton.conversations.turn.completion_delivery import COMPLETION_TOOL_NAME, DELIVER_TOOL_COMPLETION_NAME
from tai42_skeleton.interactions import ask
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.ask import wait as wait_module
from tai42_skeleton.interactions.ask.errors import InteractionTimeoutError
from tai42_skeleton.interactions.run_completion import run_completion_id

from ._continuation_support import configure_interactions_store, make_wired

RUN_DELIVERY = "rd-ask"
RUN_ID = run_completion_id(RUN_DELIVERY)
ADDRESS = "+15550002222"


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


@pytest.fixture
def calls(monkeypatch) -> list[tuple[Any, ...]]:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(working_signal, "question_sent", lambda *args: seen.append(("sent", *args)))
    monkeypatch.setattr(working_signal, "question_answered", lambda *args: seen.append(("answered", *args)))
    monkeypatch.setattr(working_signal, "question_withdrawn", lambda *args: seen.append(("withdrawn", *args)))
    return seen


@pytest.fixture
def in_run():
    with run_delivery(RunDelivery(RUN_DELIVERY, None)):
        yield


# --- the run the asking work owes a reply to ---------------------------------------------------


@pytest.mark.parametrize("completion_tool", [COMPLETION_TOOL_NAME, DELIVER_TOOL_COMPLETION_NAME])
def test_a_turns_record_names_the_run_before_its_delivery_identity(completion_tool):
    assert working_signal.current_run_id() is None
    with run_delivery(RunDelivery(RUN_DELIVERY, None)):
        assert working_signal.current_run_id() == RUN_ID
        token = set_park_completion(completion_tool, {"message_id": "turn-1"})
        try:
            assert working_signal.current_run_id() == "turn-1"
        finally:
            reset_park_completion(token)


def test_another_tools_completion_context_never_names_the_run():
    with run_delivery(RunDelivery(RUN_DELIVERY, None)):
        token = set_park_completion("probe_deliver", {"message_id": "turn-1"})
        try:
            assert working_signal.current_run_id() == RUN_ID
        finally:
            reset_park_completion(token)


# --- every question the run waits on ---------------------------------------------------------


class _RefusingChannel(DeliverOnlyChannel):
    """Refuses every recipient before anything is sent."""

    def resolve_address(self, recipient: str | None, sender_identity: str | None) -> ChannelAddress:
        raise ValueError("no such recipient")

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:
        raise NotImplementedError


@pytest.fixture
def register_channel():
    app._channel_registry.reset()

    def _register(name: str, channel: Any) -> Any:
        tai42_app.channels.register(name, channel)
        return channel

    with tai42_app.bound(app):
        yield _register
    app._channel_registry.reset()


@pytest.fixture
def driver():
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    with tool_call_frame():
        yield
    reset_execution_identity(id_token)
    reset_resume_continuation_tool(tool_token)


async def _answer_when_asked(wired) -> None:
    interaction_id, group_id = await await_add_event(wired.fake, wired.store)
    response = InteractionResponse(
        interaction_id=interaction_id, answer="yes", answered_by="u", answered_at=datetime.now(UTC)
    )
    assert await wired.store.record_answer(wired.fake, response, group_id, reply_ttl=60) is True


async def test_a_sync_ask_with_no_channel_pauses_the_signal_until_its_answer(wired, calls, in_run):
    answerer = asyncio.create_task(_answer_when_asked(wired))
    await ask("proceed?", timeout=5)
    await answerer

    assert [call[0] for call in calls] == ["sent", "answered"]
    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("answered", RUN_ID, interaction_id, None)]


async def test_an_async_ask_pauses_the_signal_and_leaves_its_question_standing(wired, calls, driver):
    with run_delivery(RunDelivery(RUN_DELIVERY, None)):
        parked = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))

    assert isinstance(parked, SuspendedInteraction)
    assert calls == [("sent", RUN_ID, parked.interaction_id)]


async def test_a_channel_ask_whose_delivery_fails_withdraws_its_question(
    wired, calls, in_run, register_channel, monkeypatch
):
    settings = wired.settings.model_copy(update={"public_base_url": "https://host.example"})
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    register_channel("line", _RefusingChannel())
    with pytest.raises(ValueError, match="no such recipient"):
        await ask("proceed?", channel="line", recipient=ADDRESS, timeout=5)

    assert [call[0] for call in calls] == ["sent", "withdrawn"]
    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("withdrawn", RUN_ID, interaction_id)]


async def test_a_sync_ask_whose_reply_read_fails_withdraws_its_question(wired, calls, in_run, monkeypatch):
    async def _blpop(*args, **kwargs):
        raise ConnectionError("reply read failed")

    monkeypatch.setattr(wired.fake, "blpop", _blpop)
    with pytest.raises(ConnectionError, match="reply read failed"):
        await ask("proceed?", timeout=5)

    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("withdrawn", RUN_ID, interaction_id)]


# --- the wait for the answer --------------------------------------------------------------------


async def _answer_from_message_when_asked(wired, medium_message_id: str, quoting: str | None = None) -> None:
    interaction_id, group_id = await await_add_event(wired.fake, wired.store)
    response = InteractionResponse(
        interaction_id=interaction_id,
        answer="yes",
        answered_by="u",
        answered_at=datetime.now(UTC),
        medium_message_id=medium_message_id,
        reply_to_medium_message_id=quoting,
    )
    assert await wired.store.record_answer(wired.fake, response, group_id, reply_ttl=60) is True


async def test_an_answer_tells_the_signal_with_the_answering_messages_id(wired, calls, in_run):
    answerer = asyncio.create_task(_answer_from_message_when_asked(wired, "M-9"))
    await ask("proceed?", timeout=5)
    await answerer

    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("answered", RUN_ID, interaction_id, "M-9")]


async def test_a_sync_ask_whose_answer_quote_read_fails_withdraws_its_question(wired, calls, in_run, monkeypatch):
    async def _quote_read_fails(interaction_id: str, medium_message_id: str) -> Any:
        raise ConnectionError("quote read failed")

    monkeypatch.setattr(wait_module, "resolve_ask_reply_to", _quote_read_fails)
    answerer = asyncio.create_task(_answer_from_message_when_asked(wired, "M-9", quoting="M-1"))
    with pytest.raises(ConnectionError, match="quote read failed"):
        await ask("proceed?", timeout=5)
    await answerer

    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("withdrawn", RUN_ID, interaction_id)]


async def test_a_sync_ask_that_times_out_withdraws_its_question(wired, calls, in_run):
    with pytest.raises(InteractionTimeoutError):
        await ask("proceed?", timeout=0.0005)

    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("withdrawn", RUN_ID, interaction_id)]


async def test_a_cancelled_sync_ask_withdraws_its_question(wired, calls, in_run, monkeypatch):
    waiting = asyncio.Event()

    async def _blpop(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(wired.fake, "blpop", _blpop)
    task = asyncio.create_task(ask("proceed?", timeout=30))
    await waiting.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    interaction_id = calls[0][2]
    assert calls == [("sent", RUN_ID, interaction_id), ("withdrawn", RUN_ID, interaction_id)]
