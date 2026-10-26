"""An ask's channel delivery is one ``ask`` send record, ended once by its attempt history.

The record is written before the first attempt, completed from the attempt that succeeded, and
otherwise ended ``failed`` when every attempt raised or ``outcome_unknown`` when any attempt may
have reached the participant (a timed-out attempt, or a cancellation while an attempt was in
flight). A reply over the channel is written onto it as ``ask_reply``; an operator answer and an
expiry write nothing. The conversations stores run the real script text on ``fakeredis[lua]``;
the interaction store runs on the interactions fake.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification, SendOutcome
from tai42_contract.conversations import ReplyTo, ReplyToStatus
from tai42_contract.interactions import AnswerFormat, InteractionRequest, InteractionResponse, ResumeItem
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_kit.utils.state_context import state_context
from tests._helpers import AddressingChannel

from tai42_skeleton.conversations.models import AskReply, AskSend, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.thread_ids import route_thread_id
from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.ask import delivery as ask_delivery
from tai42_skeleton.interactions.ask import wait as wait_module
from tai42_skeleton.interactions.ask.timing import DeadlineWindow
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import interactions as interaction_ops
from tai42_skeleton.tools.binding.surface import remove_surface_tool

from .conftest import LuaEnv, _channel_route, create_record, keys_naming

_IDENTITY = "+15550001111"
_ADDRESS = "+15550002222"
_INTERACTION = "i-1"
_GROUP = "g-1"
_CALLER_CONTEXT = StateContext(
    door="conversation",
    candidates=SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA"}),
    actor="u-1",
    turn_id="t-1",
)


def _router():
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import app as skeleton_app

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import interactions as router

    return router


def _callback():
    _router()
    return importlib.import_module("tai42_skeleton.routers.interactions.callback")


class _AskChannel(AddressingChannel):
    """A channel whose delivery attempts follow ``script``: ``ok``, ``raise``, ``timeout`` or ``block``.

    ``during`` runs inside each attempt before its step; a ``block`` step sets ``entered`` and waits
    on ``release`` before it answers.
    """

    def __init__(self, *script: str, during=None) -> None:
        self.script = list(script)
        self.calls = 0
        self.during = during
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:
        self.calls += 1
        step = self.script.pop(0)
        if self.during is not None:
            await self.during(self.calls)
        if step == "raise":
            raise ChannelDeliveryError(f"transient 503 on attempt {self.calls}", retryable=True)
        if step == "timeout":
            raise TimeoutError
        if step == "block":
            self.entered.set()
            await self.release.wait()
        return self.outcome(delivery, [f"ask-{self.calls}"])

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
        raise NotImplementedError


@pytest.fixture
def interactions(monkeypatch, fake_redis, fake_client_ctx):
    """The interaction store, every door that touches it pointed at the one interactions fake."""
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    settings = InteractionsSettings(
        public_base_url="https://cb.example", delivery_max_attempts=3, delivery_retry_backoff_seconds=0.01
    )
    for module in (helper_module, _router(), continuation_module, interaction_ops):
        monkeypatch.setattr(module, "client_ctx", fake_client_ctx)
        monkeypatch.setattr(module, "interactions_settings", lambda: settings)

    async def _no_event(**kwargs):
        return None

    monkeypatch.setattr(ask_delivery, "emit_delivery_failed", _no_event)
    return SimpleNamespace(settings=settings, store=InteractionStore(settings.key_prefix), fake=fake_redis)


def _window(seconds: float) -> DeadlineWindow:
    now = datetime.now(UTC)
    return DeadlineWindow(
        budget=seconds,
        created_at=now,
        timeout_at=now + timedelta(seconds=seconds),
        deadline=asyncio.get_running_loop().time() + seconds,
    )


async def _park(interactions, window: DeadlineWindow, *, expires: bool = False) -> None:
    resumed_on_expiry = {
        "mode": "async",
        "on_expiry": "resume",
        "expiry_at": window.timeout_at,
        "continuation_tool": "resume_tool",
        "continuation_identity": "svc-key",
        "continuation_state_context": _CALLER_CONTEXT,
    }
    request = InteractionRequest(
        interaction_id=_INTERACTION,
        group_id=_GROUP,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=interactions.store.reply_key(_INTERACTION),
        created_at=window.created_at,
        timeout_at=window.timeout_at,
        **(resumed_on_expiry if expires else {}),
    )
    await interactions.store.add(interactions.fake, request, idle_ttl=86400)


async def _ask(
    lua_env: LuaEnv | None,
    interactions,
    channel: _AskChannel,
    *,
    seconds: float = 30,
    sensitive: bool = False,
    expires: bool = False,
    parked: bool = True,
    timed_out: bool = False,
    before=None,
):
    if lua_env is not None:
        await lua_env.manager.put_route(_channel_route())
    window = _window(seconds)
    if parked:
        await _park(interactions, window, expires=expires)
    if before is not None:
        await before()
    frame = ChannelDelivery(
        interaction_id=_INTERACTION,
        question="proceed?",
        answer_format="text",
        sensitive=sensitive,
        callback_url="https://cb.example/api/interactions/callback/tk",
        timeout_at=datetime.now(UTC) - timedelta(seconds=1) if timed_out else window.timeout_at,
    )
    await ask_delivery.deliver_with_retry(
        channel,
        frame,
        interactions.store,
        window,
        channel="twilio",
        recipient=_ADDRESS,
        interaction_id=_INTERACTION,
        group=_GROUP,
        question="proceed?",
        sensitive=sensitive,
    )


async def _the_record(lua_env: LuaEnv) -> ConversationRecord:
    message_id = await lua_env.redis.get(lua_env.store.settings.ask_send_key(_INTERACTION))
    assert message_id is not None
    record = await lua_env.store.get_record(message_id)
    assert record is not None
    return record


async def _claim_over_the_channel(
    interactions, answer, medium_message_id: str | None = "reply-mid-1", reply_to_medium_message_id: str | None = None
):
    state = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert state is not None
    return await _router()._record_callback_answer(
        interactions.fake,
        interactions.store,
        interactions.settings,
        "tkt",
        _INTERACTION,
        state,
        answer,
        medium_message_id=medium_message_id,
        reply_to_medium_message_id=reply_to_medium_message_id,
    )


async def test_an_ask_is_one_ask_record_completed_from_its_outcome_with_its_ask_index(lua_env, interactions):
    seen: list = []

    async def during(call):
        record = await _the_record(lua_env)
        key = lua_env.store.settings.status_index_key(DeliveryStatus.SENDING.value)
        seen.append((record, await lua_env.redis.zscore(key, record.message_id), time.time()))

    channel = _AskChannel("ok", during=during)
    await _ask(lua_env, interactions, channel, seconds=30)

    [(sending, score, _)] = seen
    assert sending.delivery_status is DeliveryStatus.SENDING
    assert sending.sent_by == "ask"
    lease = lua_env.store.settings.delivery_claim_lease_seconds
    assert score == pytest.approx(sending.created_at + 30 + lease, abs=1.0)
    record = await _the_record(lua_env)
    assert record.message_id == sending.message_id
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert [sent.provider_message_ids for sent in record.sends] == [["ask-1"]]
    assert record.ask == AskSend(interaction_id=_INTERACTION, answer_format="text", sensitive=False, attempts=1)
    assert record.thread_id == route_thread_id("line", _ADDRESS)


async def test_an_ask_whose_every_attempt_raises_ends_one_failed_record(lua_env, interactions):
    channel = _AskChannel("raise", "raise", "raise")

    with pytest.raises(ChannelDeliveryError, match="attempt 3"):
        await _ask(lua_env, interactions, channel)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.FAILED
    assert "attempt 3" in (record.error or "")
    assert record.ask is not None
    assert record.ask.attempts == 3
    assert record.ask.timed_out_attempts == []


async def test_an_ask_that_timed_out_then_succeeded_is_completed_and_warns_of_a_duplicate(
    lua_env, interactions, caplog
):
    channel = _AskChannel("timeout", "ok")

    with caplog.at_level(logging.WARNING):
        await _ask(lua_env, interactions, channel)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert [sent.provider_message_ids for sent in record.sends] == [["ask-2"]]
    assert record.ask is not None
    assert record.ask.attempts == 2
    assert record.ask.timed_out_attempts == [1]
    assert any(record.message_id in message and "more than once" in message for message in caplog.messages)


@pytest.mark.parametrize("script", [("raise", "raise", "timeout"), ("timeout", "raise", "raise")])
async def test_an_ask_with_a_timed_out_attempt_and_no_success_ends_outcome_unknown(
    script, lua_env, interactions, caplog
):
    channel = _AskChannel(*script)

    with caplog.at_level(logging.WARNING), pytest.raises(ChannelDeliveryError):
        await _ask(lua_env, interactions, channel)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
    assert record.ask is not None
    assert record.ask.timed_out_attempts == [script.index("timeout") + 1]
    assert any(record.message_id in message and "may have received" in message for message in caplog.messages)


async def _cancel(task: asyncio.Task, until) -> None:
    await until()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_cancellation_while_an_attempt_is_in_flight_ends_outcome_unknown(lua_env, interactions):
    channel = _AskChannel("block")
    task = asyncio.ensure_future(_ask(lua_env, interactions, channel))

    await _cancel(task, channel.entered.wait)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
    assert record.ask is not None
    assert record.ask.timed_out_attempts == []
    assert "in flight" in (record.error or "")


@pytest.mark.parametrize(
    ("first", "status", "timed_out"),
    [("raise", DeliveryStatus.FAILED, []), ("timeout", DeliveryStatus.OUTCOME_UNKNOWN, [1])],
)
async def test_a_cancellation_in_the_backoff_ends_by_the_attempt_history(
    first, status, timed_out, lua_env, interactions, monkeypatch, caplog
):
    slow = interactions.settings.model_copy(update={"delivery_retry_backoff_seconds": 30})
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: slow)
    attempted = asyncio.Event()

    async def during(call):
        attempted.set()

    channel = _AskChannel(first, "ok", during=during)
    task = asyncio.ensure_future(_ask(lua_env, interactions, channel, seconds=600))

    async def in_the_backoff():
        await attempted.wait()
        for _ in range(20):
            await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING):
        await _cancel(task, in_the_backoff)

    assert channel.calls == 1
    record = await _the_record(lua_env)
    assert record.delivery_status is status
    assert record.ask is not None
    assert record.ask.attempts == 1
    assert record.ask.timed_out_attempts == timed_out
    if timed_out:
        assert any(record.message_id in message and "may have received" in message for message in caplog.messages)


async def test_an_ask_past_its_timeout_whose_question_is_gone_ends_failed_with_nothing_sent(lua_env, interactions):
    channel = _AskChannel("ok")

    await _ask(lua_env, interactions, channel, parked=False, timed_out=True)

    record = await _the_record(lua_env)
    assert channel.calls == 0
    assert record.delivery_status is DeliveryStatus.FAILED
    assert record.error == "no delivery attempt went out: the question was answered or gone first"
    assert record.sends == []
    assert record.ask == AskSend(interaction_id=_INTERACTION, answer_format="text", sensitive=False, attempts=0)


async def test_an_ask_past_its_timeout_answered_first_ends_failed_with_nothing_sent(lua_env, interactions):
    channel = _AskChannel("ok")

    async def answer_first():
        state = await interactions.store.get_state(interactions.fake, _INTERACTION)
        assert state is not None
        assert (await _claim_over_the_channel(interactions, "yes")).status_code == 200

    await _ask(lua_env, interactions, channel, timed_out=True, before=answer_first)

    record = await _the_record(lua_env)
    assert channel.calls == 0
    assert record.delivery_status is DeliveryStatus.FAILED
    assert record.ask_reply is None
    assert record.ask is not None
    assert record.ask.attempts == 0


async def test_a_reply_that_landed_before_the_last_attempt_failed_ends_delivered(lua_env, interactions):
    statuses: list[DeliveryStatus] = []

    async def during(call):
        if call == 3:
            response = await _claim_over_the_channel(interactions, "yes")
            assert response.status_code == 200
        statuses.append((await _the_record(lua_env)).delivery_status)

    channel = _AskChannel("raise", "raise", "raise", during=during)
    await _ask(lua_env, interactions, channel)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.error is None
    assert record.ask_reply is not None
    assert record.ask_reply.answer == "yes"
    assert record.ask_reply.medium_message_id == "reply-mid-1"
    assert DeliveryStatus.FAILED not in statuses


async def test_an_operator_answer_writes_nothing_to_the_ask_record(lua_env, interactions):
    channel = _AskChannel("raise", "raise", "raise")
    with pytest.raises(ChannelDeliveryError):
        await _ask(lua_env, interactions, channel)
    failed = await _the_record(lua_env)
    await interactions.store.add(
        interactions.fake,
        InteractionRequest(
            interaction_id=_INTERACTION,
            group_id=_GROUP,
            question="proceed?",
            answer_format=AnswerFormat.TEXT,
            reply_to=interactions.store.reply_key(_INTERACTION),
            created_at=datetime.now(UTC),
            timeout_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        idle_ttl=86400,
    )

    assert await interaction_ops.answer_interaction(_INTERACTION, "yes") == {
        "interaction_id": _INTERACTION,
        "status": "answered",
    }

    after = await _the_record(lua_env)
    assert after == failed
    assert after.delivery_status is DeliveryStatus.FAILED
    assert after.ask_reply is None


async def test_an_operator_answer_leaves_a_provisional_ask_record_provisional(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    sent = await _the_record(lua_env)

    await interaction_ops.answer_interaction(_INTERACTION, "yes")

    after = await _the_record(lua_env)
    assert after == sent
    assert after.delivery_status is DeliveryStatus.PROVISIONAL


@pytest.mark.parametrize("script", [("raise", "raise", "raise"), ("raise", "raise", "timeout")])
async def test_a_reply_after_the_ask_ended_without_an_outcome_moves_it_to_delivered(script, lua_env, interactions):
    with pytest.raises(ChannelDeliveryError):
        await _ask(lua_env, interactions, _AskChannel(*script))
    ended = await _the_record(lua_env)
    assert ended.error is not None

    reply = AskReply(answer="yes", medium_message_id="reply-mid-1", answered_at=datetime.now(UTC))
    assert await lua_env.store.record_ask_reply(_INTERACTION, reply) == ("recorded", ended.message_id)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.error is None
    assert record.ask_reply == reply


async def test_a_reply_on_a_provisional_ask_moves_it_to_delivered(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))

    response = await _claim_over_the_channel(interactions, "yes")

    assert response.status_code == 200
    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.ask_reply is not None
    assert record.ask_reply.answer == "yes"
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "reply-mid-1") == [record.message_id]


@pytest.mark.parametrize("script", [("raise", "raise", "raise"), ("ok",)])
async def test_a_reply_on_a_sending_ask_is_kept_and_its_end_writes_delivered(script, lua_env, interactions):
    async def during(call):
        if call == len(script):
            reply = AskReply(answer="yes", medium_message_id=None, answered_at=datetime.now(UTC))
            outcome, _ = await lua_env.store.record_ask_reply(_INTERACTION, reply)
            assert outcome == "recorded"
            assert (await _the_record(lua_env)).delivery_status is DeliveryStatus.SENDING

    channel = _AskChannel(*script, during=during)
    if script[-1] == "raise":
        with pytest.raises(ChannelDeliveryError):
            await _ask(lua_env, interactions, channel)
    else:
        await _ask(lua_env, interactions, channel)

    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.error is None
    assert record.ask_reply is not None


async def test_an_external_reply_claimed_at_the_callback_door_sets_ask_reply(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    state = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert state is not None

    response = await _callback()._claim_external(
        interactions.fake, interactions.store, interactions.settings, "tkt", _INTERACTION, state, {"ok": 1}
    )

    assert response.status_code == 200
    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.ask_reply is not None
    assert record.ask_reply.answer == {"ok": 1}
    assert record.ask_reply.medium_message_id is None


@pytest.mark.parametrize("sensitive", [True, False])
async def test_a_reply_to_a_sensitive_ask_is_recorded_without_its_body(sensitive, lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"), sensitive=sensitive)

    response = await _claim_over_the_channel(interactions, "s3cret-reply")

    assert response.status_code == 200
    record = await _the_record(lua_env)
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert record.ask is not None
    assert record.ask.sensitive is sensitive
    assert record.ask_reply is not None
    assert record.ask_reply.medium_message_id == "reply-mid-1"
    body = None if sensitive else "s3cret-reply"
    assert record.ask_reply.answer == body
    page, unreadable = await lua_env.store.search_route_messages("line", offset=0, limit=10, q="s3cret-reply")
    assert unreadable == 0
    assert [found.message_id for found in page.records] == ([] if sensitive else [record.message_id])
    assert ("s3cret-reply" in json.dumps(record.view())) is not sensitive
    assert ("s3cret-reply" in json.dumps(record.caller_view())) is not sensitive
    assert record.caller_view()["ask_reply"] == {"answer": body, "reply_to": None}
    [referenced] = await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "reply-mid-1")
    quoted = await lua_env.store.get_record(referenced)
    assert quoted is not None
    assert quoted.ask_reply is not None
    assert quoted.ask_reply.answer == body


async def test_a_channel_reply_carries_its_medium_id_onto_the_stored_response(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))

    state = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert state is not None
    response = await _router()._record_callback_answer(
        interactions.fake,
        interactions.store,
        interactions.settings,
        "tkt",
        _INTERACTION,
        state,
        "yes",
        medium_message_id="reply-mid-1",
    )

    assert response.status_code == 200
    answered = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert answered is not None
    assert answered.response is not None
    assert answered.response.medium_message_id == "reply-mid-1"


async def test_a_channel_reply_carries_both_its_medium_ids_onto_the_stored_response(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))

    response = await _claim_over_the_channel(interactions, "yes", reply_to_medium_message_id="ask-1")

    assert response.status_code == 200
    state = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert state is not None
    assert state.response is not None
    assert state.response.medium_message_id == "reply-mid-1"
    assert state.response.reply_to_medium_message_id == "ask-1"


@pytest.mark.parametrize("sensitive", [True, False])
async def test_a_reply_quoting_the_ask_records_the_quote_on_its_ask_reply(sensitive, lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"), sensitive=sensitive)

    response = await _claim_over_the_channel(interactions, "yes", reply_to_medium_message_id="ask-1")

    assert response.status_code == 200
    record = await _the_record(lua_env)
    assert record.ask_reply is not None
    quote = ReplyTo(message_id="ask-1", status=ReplyToStatus.FOUND, text="proceed?")
    assert record.ask_reply.reply_to == quote
    caller_reply = record.caller_view()["ask_reply"]
    assert isinstance(caller_reply, dict)
    assert caller_reply["reply_to"] == quote.model_dump(mode="json")


async def _seed_quoted(lua_env: LuaEnv) -> None:
    """Record, in the ask's thread, an inbound message ``mid-quoted`` whose text a reply can quote."""
    asked = await _the_record(lua_env)
    now = time.time()
    quoted = asked.model_copy(
        update={
            "message_id": "m-quoted",
            "origin": "client",
            "sent_by": None,
            "ask": None,
            "sends": [],
            "inbound_text": "the words quoted back",
            "inbound_message_id": "mid-quoted",
            "answer_status": "answered",
            "answer": "noted",
            "delivery_status": DeliveryStatus.DELIVERED,
            "created_at": now,
            "updated_at": now,
        }
    )
    await create_record(lua_env.store, quoted)


async def _quoting_answer_recorded(interactions) -> None:
    """Claim the ask with a reply quoting ``mid-quoted`` in the interaction store alone, so the
    waiter can wake before the callback door has written the ask record's ``ask_reply``."""
    response = InteractionResponse(
        interaction_id=_INTERACTION,
        answer="yes",
        answered_by="participant",
        answered_at=datetime.now(UTC),
        medium_message_id="mid-reply",
        reply_to_medium_message_id="mid-quoted",
    )
    assert await interactions.store.record_answer(interactions.fake, response, _GROUP, reply_ttl=60)


async def _await_the_answer(interactions):
    return await wait_module.await_answer(
        interactions.store,
        _window(5),
        interaction_id=_INTERACTION,
        group=_GROUP,
        reply_key=interactions.store.reply_key(_INTERACTION),
        question="proceed?",
        sensitive=False,
    )


_QUOTED = ReplyTo(message_id="mid-quoted", status=ReplyToStatus.FOUND, text="the words quoted back")
_UNREACHABLE = ReplyTo(message_id="mid-quoted", status=ReplyToStatus.NOT_FOUND, text=None)


async def test_a_waiter_woken_before_the_ask_reply_is_written_still_reads_the_quote(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    await _seed_quoted(lua_env)
    await _quoting_answer_recorded(interactions)

    answered = await _await_the_answer(interactions)

    assert answered.answer == "yes"
    assert answered.reply_to == _QUOTED
    assert (await _the_record(lua_env)).ask_reply is None


async def test_a_sync_ask_whose_thread_is_forgotten_while_it_waits_quotes_not_found(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    await _seed_quoted(lua_env)
    await lua_env.store.drop_thread("line", route_thread_id("line", _ADDRESS))
    await _quoting_answer_recorded(interactions)

    answered = await _await_the_answer(interactions)

    assert answered.answer == "yes"
    assert answered.reply_to == _UNREACHABLE


async def test_a_thread_forgotten_between_the_waiters_wake_and_its_read_quotes_not_found(
    lua_env, interactions, monkeypatch
):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    await _seed_quoted(lua_env)
    await _quoting_answer_recorded(interactions)
    resolve = wait_module.resolve_ask_reply_to

    async def _forget_then_read(interaction_id: str, medium_message_id: str) -> ReplyTo:
        await lua_env.store.drop_thread("line", route_thread_id("line", _ADDRESS))
        return await resolve(interaction_id, medium_message_id)

    monkeypatch.setattr(wait_module, "resolve_ask_reply_to", _forget_then_read)

    answered = await _await_the_answer(interactions)

    assert answered.reply_to == _UNREACHABLE


async def test_the_interaction_store_keeps_the_quoted_id_and_never_the_quoted_text(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    await _seed_quoted(lua_env)

    response = await _claim_over_the_channel(interactions, "yes", reply_to_medium_message_id="mid-quoted")

    assert response.status_code == 200
    record = await _the_record(lua_env)
    assert record.ask_reply is not None
    assert record.ask_reply.reply_to == ReplyTo(
        message_id="mid-quoted", status=ReplyToStatus.FOUND, text="the words quoted back"
    )
    fake = interactions.fake
    stored = json.dumps(
        [fake._hashes, fake._strings, fake._lists, fake._streams, sorted(map(sorted, fake._sets.values()))],
        default=str,
    )
    assert "mid-quoted" in stored
    assert "the words quoted back" not in stored


async def test_a_reply_quoting_nothing_records_no_quote(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))

    assert (await _claim_over_the_channel(interactions, "yes")).status_code == 200

    record = await _the_record(lua_env)
    assert record.ask_reply is not None
    assert record.ask_reply.reply_to is None


async def test_a_store_fault_resolving_the_quote_leaves_the_won_claim_answered(
    lua_env, interactions, monkeypatch, caplog
):
    await _ask(lua_env, interactions, _AskChannel("ok"))

    async def _broken(*args, **kwargs):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(lua_env.store, "resolve_message_ref", _broken)
    with caplog.at_level(logging.ERROR):
        response = await _claim_over_the_channel(interactions, "yes", reply_to_medium_message_id="ask-1")

    assert response.status_code == 200
    assert "ask reply for interaction i-1 not recorded" in caplog.text
    record = await _the_record(lua_env)
    assert record.ask_reply is None


@asynccontextmanager
async def _resume_tool_app():
    from tai42_skeleton.app import instance

    async def _clear() -> None:
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(instance.app._fast_mcp, tool.name)

    await _clear()
    try:
        manifest = Manifest.model_validate(
            {"tools": [{"title": "resume", "module": "tests.app._fixtures.resume_tool"}]}
        )
        async with instance.app.app_context(manifest):
            with state_context(_CALLER_CONTEXT):
                yield
    finally:
        await _clear()


async def test_a_caller_resume_writes_nothing_to_any_send_record(lua_env, interactions, monkeypatch, fake_client_ctx):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    monkeypatch.setattr(visit_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: interactions.settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)

    @asynccontextmanager
    async def _no_identity(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _no_identity)
    now = datetime.now(UTC)
    await interactions.store.add(
        interactions.fake,
        InteractionRequest(
            interaction_id="c-1",
            group_id="g-2",
            question="proceed?",
            answer_format=AnswerFormat.FREE,
            reply_to=interactions.store.reply_key("c-1"),
            created_at=now,
            timeout_at=now + timedelta(minutes=60),
            mode="async",
            to="caller",
            continuation_tool="resume_tool",
            continuation_identity="svc-key",
            continuation_state_context=_CALLER_CONTEXT,
            expiry_at=now + timedelta(minutes=60),
        ),
        idle_ttl=86400,
        to="caller",
    )

    async def _no_reply(*args, **kwargs):
        raise AssertionError("a caller resume wrote an ask reply")

    monkeypatch.setattr(lua_env.store, "record_ask_reply", _no_reply)
    before = {key: await lua_env.redis.dump(key) for key in await lua_env.redis.keys("*")}

    async with _resume_tool_app():
        outcome = await visit_module.visit(
            target_name="t",
            cancel=[],
            resume=[ResumeItem(id="c-1", payload={"value": 1})],
            start=None,
            extras={},
        )

    assert outcome.action == "resumed"
    assert {key: await lua_env.redis.dump(key) for key in await lua_env.redis.keys("*")} == before


def _ask_record(**update) -> dict:
    now = time.time()
    fields = {
        "message_id": "m-1",
        "route_name": "line",
        "door": "channel",
        "thread_id": route_thread_id("line", _ADDRESS),
        "client_address": _ADDRESS,
        "channel": "twilio",
        "our_identity": _IDENTITY,
        "origin": "send",
        "sent_by": "ask",
        "inbound_text": "",
        "answer_status": "answered",
        "answer": "proceed?",
        "delivery_status": DeliveryStatus.DELIVERED,
        "ask": AskSend(interaction_id=_INTERACTION, answer_format="text", sensitive=False, attempts=1),
        "ask_reply": AskReply(answer="yes", answered_at=datetime.now(UTC)),
        "created_at": now,
        "updated_at": now,
    }
    fields.update(update)
    return fields


@pytest.mark.parametrize("status", [DeliveryStatus.FAILED, DeliveryStatus.OUTCOME_UNKNOWN])
def test_an_ask_reply_is_refused_on_a_record_that_ended_without_an_outcome(status):
    with pytest.raises(ValidationError, match="ask_reply"):
        ConversationRecord(**_ask_record(delivery_status=status, error="it failed"))


def test_a_sensitive_ask_refuses_a_reply_body():
    sensitive = AskSend(interaction_id=_INTERACTION, answer_format="text", sensitive=True, attempts=1)
    with pytest.raises(ValidationError, match="sensitive"):
        ConversationRecord(**_ask_record(ask=sensitive))
    ConversationRecord(**_ask_record(ask=sensitive, ask_reply=AskReply(answer=None, answered_at=datetime.now(UTC))))


async def test_an_ask_abandoned_when_its_window_ran_out_writes_nothing_to_its_record(lua_env, interactions):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    sent = await _the_record(lua_env)

    assert await ask_delivery.prune(interactions.store, _INTERACTION, _GROUP) == "pruned"

    assert await _the_record(lua_env) == sent


async def test_an_async_ask_resolved_by_expiry_writes_nothing_to_its_record(lua_env, interactions, monkeypatch):
    dispatched: list = []
    monkeypatch.setattr(reaper_module, "interactions_settings", lambda: interactions.settings)
    monkeypatch.setattr(reaper_module, "dispatch_continuation", lambda *args: dispatched.append(args))

    async def _no_event(*args, **kwargs):
        return None

    monkeypatch.setattr(reaper_module, "_emit_ask_expired_unanswered", _no_event)
    await _ask(lua_env, interactions, _AskChannel("ok"), expires=True)
    sent = await _the_record(lua_env)
    state = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert state is not None
    assert state.request.expiry_at is not None

    resolved = await reaper_module._reap_one_expired_park(
        interactions.fake, interactions.store, _INTERACTION, state.request.expiry_at + timedelta(seconds=1)
    )

    assert resolved
    assert len(dispatched) == 1
    assert await _the_record(lua_env) == sent


async def test_with_no_conversations_store_an_ask_still_delivers_and_its_reply_is_claimed(no_store, interactions):
    channel = _AskChannel("ok")

    await _ask(None, interactions, channel)

    assert channel.calls == 1
    response = await _claim_over_the_channel(interactions, "yes")
    assert response.status_code == 200
    state = await interactions.store.get_state(interactions.fake, _INTERACTION)
    assert state is not None
    assert state.status == "answered"
    assert no_store == []


async def test_an_ask_forgotten_in_its_retry_loop_is_deleted_by_its_end_and_a_later_reply_writes_nothing(
    lua_env, interactions, caplog
):
    seen: dict = {}

    async def during(call):
        if call == 1:
            seen["mid"] = (await _the_record(lua_env)).message_id
            await lua_env.store.drop_thread("line", route_thread_id("line", _ADDRESS))
            assert await lua_env.redis.get(lua_env.store.settings.ask_send_key(_INTERACTION)) == seen["mid"]
        if call == 2:
            assert await lua_env.redis.get(lua_env.store.settings.ask_send_key(_INTERACTION)) == seen["mid"]

    with caplog.at_level(logging.WARNING):
        await _ask(lua_env, interactions, _AskChannel("raise", "ok", during=during))

    mid = seen["mid"]
    assert await lua_env.redis.exists(lua_env.store.settings.record_key(mid)) == 0
    assert await lua_env.redis.exists(lua_env.store.settings.ask_send_key(_INTERACTION)) == 0
    assert await keys_naming(lua_env, mid) == set()
    assert any(mid in message and "forgotten" in message for message in caplog.messages)

    response = await _claim_over_the_channel(interactions, "yes")

    assert response.status_code == 200
    assert await lua_env.redis.exists(lua_env.store.settings.record_key(mid)) == 0
    assert await keys_naming(lua_env, mid) == set()


async def test_a_reply_whose_ask_record_is_gone_is_a_warning_naming_the_interaction_and_the_record(
    lua_env, interactions, caplog
):
    await _ask(lua_env, interactions, _AskChannel("ok"))
    record = await _the_record(lua_env)
    await lua_env.redis.delete(lua_env.store.settings.record_key(record.message_id))

    with caplog.at_level(logging.WARNING):
        response = await _claim_over_the_channel(interactions, "yes")

    assert response.status_code == 200
    assert any(
        _INTERACTION in message and record.message_id in message and "gone" in message for message in caplog.messages
    )
