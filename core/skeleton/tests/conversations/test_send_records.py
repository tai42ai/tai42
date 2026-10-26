"""Every platform send outside a turn's reply is a send record, written before the medium call.

The record exists in ``sending`` while the medium is called, indexed by its settle deadline and in
no thread; the outcome completes it into its thread (``provisional`` with its sends, outbound index
and message refs), a raise ends it ``failed``, and a send past its bound, a cancellation, or a stopped
process ends it ``outcome_unknown`` — never re-sent. The stores run the real script text on
``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from tai42_contract.channels import ChannelDeliveryError, InboundBridge, SendOutcome
from tai42_contract.conversations import (
    AnswerPart,
    ConversationTurnRef,
    InboundRejectionReason,
    TargetConversationConfig,
    reset_conversation_turn,
    set_conversation_turn,
)

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.app.instance import app as skeleton_app
from tai42_skeleton.channels import inbound as inbound_module
from tai42_skeleton.channels import inbound_rejection as inbound_rejection_module
from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import sends as sends_module
from tai42_skeleton.conversations.delivery_channel import _notify_refusal
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.record_send import SETTLE_ERROR
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id

from .conftest import (
    FakeChannel,
    GatedChannel,
    LuaEnv,
    _channel_route,
    _FakeApp,
    create_record,
    past_deadline,
    send_records,
    sending_ids,
    standing_person,
)

_IDENTITY = "+15550001111"
_ADDRESS = "+15550002222"


@pytest.fixture(autouse=True)
def grace_confirms(monkeypatch):
    """Collect the grace confirmations a completed send schedules instead of running them."""
    spawned: list = []

    def _spawn(coro):
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(delivery_module, "_spawn", _spawn)
    return spawned


@pytest.fixture
def channels(monkeypatch):
    """Install a fake as the ``twilio`` channel ``notify_user`` resolves."""

    def _register(channel):
        def _resolve(name):
            assert name == "twilio"
            return channel

        monkeypatch.setattr(notify_module, "resolve_channel", _resolve)
        return channel

    return _register


async def _routed(lua_env: LuaEnv) -> None:
    await lua_env.manager.put_route(_channel_route())


async def test_the_record_is_sending_before_the_medium_call_and_in_no_thread(lua_env, channels):
    await _routed(lua_env)

    async def during():
        [record] = await send_records(lua_env)
        [(member, score)] = await sending_ids(lua_env)
        thread_key = lua_env.store.settings.keyspace.thread_index_key("line", route_thread_id("line", _ADDRESS))
        return record, member, score, await lua_env.redis.zrange(thread_key, 0, -1)

    channel = channels(GatedChannel(during))
    channel.release.set()
    before = time.time()
    await notify_user("hello", channel="twilio", recipient=_ADDRESS)

    record, member, score, thread_members = channel.seen
    settings = lua_env.store.settings
    assert record.delivery_status is DeliveryStatus.SENDING
    assert record.sent_by == "notify"
    assert member == record.message_id
    expected = record.created_at + settings.delivery_send_timeout_seconds + settings.delivery_claim_lease_seconds
    assert score == pytest.approx(expected, abs=1e-3)
    assert record.created_at >= before
    assert thread_members == []


def _thread_key(lua_env: LuaEnv, thread_id: str, route_name: str = "line") -> str:
    return lua_env.store.settings.keyspace.thread_index_key(route_name, thread_id)


async def test_a_completed_send_is_provisional_with_its_sends_outbound_index_and_refs_in_its_thread(
    lua_env, channels, grace_confirms
):
    await _routed(lua_env)
    channels(FakeChannel())

    assert await notify_user("hello", channel="twilio", recipient=_ADDRESS) == ["out-1"]

    [record] = await send_records(lua_env)
    settings = lua_env.store.settings
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert [sent.provider_message_ids for sent in record.sends] == [["out-1"]]
    assert record.answer == "hello"
    assert record.thread_id == route_thread_id("line", _ADDRESS)
    assert await lua_env.redis.get(settings.keyspace.outbound_index_key("twilio", "out-1")) == record.message_id
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "out-1") == [record.message_id]
    assert await lua_env.redis.zrange(_thread_key(lua_env, record.thread_id), 0, -1) == [record.message_id]
    assert await sending_ids(lua_env) == []
    assert len(grace_confirms) == 1


async def test_a_failing_sending_write_raises_and_nothing_is_sent(lua_env, channels, monkeypatch):
    await _routed(lua_env)
    channel = channels(FakeChannel())

    async def _down(*args, **kwargs):
        raise ConnectionError("records redis down")

    monkeypatch.setattr(lua_env.store, "begin_send_record", _down)

    with pytest.raises(ConnectionError, match="records redis down"):
        await notify_user("hello", channel="twilio", recipient=_ADDRESS)
    assert channel.sends == []
    assert await send_records(lua_env) == []


@pytest.fixture
def short_bound(monkeypatch):
    """A send bound short enough for a test to pass it; requested before ``lua_env`` reads the settings."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_SEND_TIMEOUT_SECONDS", "0.05")


async def test_a_send_past_its_bound_ends_outcome_unknown_and_raises_not_retryable(
    short_bound, lua_env, channels, caplog
):
    await _routed(lua_env)
    channels(GatedChannel())

    with caplog.at_level(logging.WARNING), pytest.raises(ChannelDeliveryError) as raised:
        await notify_user("hello", channel="twilio", recipient=_ADDRESS)

    assert raised.value.retryable is False
    [record] = await send_records(lua_env)
    assert record.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
    assert "did not answer within" in (record.error or "")
    assert any(record.message_id in message and "not re-sent" in message for message in caplog.messages)
    assert await sending_ids(lua_env) == []


@pytest.mark.parametrize("settle", ["sweep", "redrive"])
async def test_a_stranded_sending_row_past_its_deadline_settles_outcome_unknown_once(
    settle, lua_env, channels, monkeypatch, caplog
):
    await _routed(lua_env)
    channel = channels(FakeChannel())

    async def _stopped(*args, **kwargs):
        return None

    monkeypatch.setattr(sends_module, "_complete", _stopped)
    await notify_user("hello", channel="twilio", recipient=_ADDRESS)
    [record] = await send_records(lua_env)
    assert record.delivery_status is DeliveryStatus.SENDING
    await past_deadline(lua_env, record.message_id)

    with caplog.at_level(logging.WARNING):
        if settle == "sweep":
            await delivery_module.sweep_stalled_deliveries()
        else:
            await delivery_module.redrive_pending()

    settled = await lua_env.store.get_record(record.message_id)
    assert settled is not None
    assert settled.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
    assert settled.error == SETTLE_ERROR
    assert any(record.message_id in message and "not re-sent" in message for message in caplog.messages)
    assert len(channel.sends) == 1
    assert await lua_env.redis.zrange(_thread_key(lua_env, record.thread_id), 0, -1) == [record.message_id]


async def test_a_sending_row_inside_its_deadline_is_left_alone_by_both_passes(lua_env, channels, monkeypatch):
    await _routed(lua_env)
    channel = channels(FakeChannel())

    async def _stopped(*args, **kwargs):
        return None

    monkeypatch.setattr(sends_module, "_complete", _stopped)
    await notify_user("hello", channel="twilio", recipient=_ADDRESS)
    [record] = await send_records(lua_env)

    await delivery_module.sweep_stalled_deliveries()
    await delivery_module.redrive_pending()

    still = await lua_env.store.get_record(record.message_id)
    assert still is not None
    assert still.delivery_status is DeliveryStatus.SENDING
    assert [member for member, _ in await sending_ids(lua_env)] == [record.message_id]
    assert len(channel.sends) == 1


async def test_deliver_on_a_sending_row_sends_nothing(lua_env, channels, monkeypatch):
    await _routed(lua_env)
    channel = channels(FakeChannel())

    async def _stopped(*args, **kwargs):
        return None

    monkeypatch.setattr(sends_module, "_complete", _stopped)
    await notify_user("hello", channel="twilio", recipient=_ADDRESS)
    [record] = await send_records(lua_env)

    assert await lua_env.store.claim_delivery(record.message_id, time.time(), "tok", 30) == 0
    await delivery_module.deliver(record.message_id)

    assert len(channel.sends) == 1
    still = await lua_env.store.get_record(record.message_id)
    assert still is not None
    assert still.delivery_status is DeliveryStatus.SENDING


async def test_a_failed_completion_write_logs_error_marks_the_span_returns_the_ids_and_settles_later(
    lua_env, channels, monkeypatch, caplog
):
    await _routed(lua_env)
    channels(FakeChannel())
    span_updates: list[dict] = []
    writer = SimpleNamespace(update_current_span=lambda **kwargs: span_updates.append(kwargs))
    monkeypatch.setattr(sends_module, "get_monitoring", lambda: SimpleNamespace(writer=writer))

    async def _down(*args, **kwargs):
        raise ConnectionError("records redis down")

    monkeypatch.setattr(lua_env.store, "complete_send_record", _down)

    with caplog.at_level(logging.ERROR):
        assert await notify_user("hello", channel="twilio", recipient=_ADDRESS) == ["out-1"]

    [record] = await send_records(lua_env)
    assert record.delivery_status is DeliveryStatus.SENDING
    [error] = [entry for entry in caplog.records if entry.levelno == logging.ERROR]
    assert record.message_id in error.getMessage()
    assert "'out-1'" in error.getMessage()
    [update] = span_updates
    assert update["level"].value == "ERROR"
    assert record.message_id in update["status_message"]

    await past_deadline(lua_env, record.message_id)
    await lua_env.store.settle_due_sends()
    settled = await lua_env.store.get_record(record.message_id)
    assert settled is not None
    assert settled.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN


# -- attribution ----------------------------------------------------------------


async def _lead(lua_env: LuaEnv, address: str = _ADDRESS) -> ConversationRecord:
    now = time.time()
    lead = ConversationRecord(
        message_id="lead-1",
        route_name="line",
        door="channel",
        thread_id=route_thread_id("line", address),
        client_address=address,
        channel="twilio",
        our_identity=_IDENTITY,
        provider_message_id="inbound-1",
        origin="client",
        inbound_text="hi",
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )
    await create_record(lua_env.store, lead, route=_channel_route(), intake_token="tok")
    return lead


async def test_notify_inside_a_turn_lands_in_the_turn_thread_naming_the_turn(lua_env, channels):
    await _routed(lua_env)
    lead = await _lead(lua_env)
    channels(FakeChannel())
    token = set_conversation_turn(ConversationTurnRef(thread_id=lead.thread_id, message_id="lead-1", route_name="line"))
    try:
        await notify_user("from the turn", channel="twilio", recipient=_ADDRESS)
    finally:
        reset_conversation_turn(token)

    [record] = await send_records(lua_env)
    assert record.thread_id == lead.thread_id
    assert record.turn_message_id == "lead-1"
    assert record.answer == "from the turn"
    assert [sent.provider_message_ids for sent in record.sends] == [["out-1"]]
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, _ADDRESS, "out-1") == [record.message_id]
    members = await lua_env.redis.zrange(_thread_key(lua_env, lead.thread_id), 0, -1)
    assert set(members) == {"lead-1", record.message_id}


async def test_notify_outside_a_turn_creates_the_route_thread_of_its_address(lua_env, channels):
    await _routed(lua_env)
    channels(FakeChannel())

    await notify_user("hello", channel="twilio", recipient=_ADDRESS)

    [record] = await send_records(lua_env)
    thread_id = route_thread_id("line", _ADDRESS)
    assert record.thread_id == thread_id
    assert record.turn_message_id is None
    assert await lua_env.store.thread_exists("line", thread_id)


async def test_notify_to_a_linked_person_lands_in_the_person_thread(lua_env, channels):
    await _routed(lua_env)
    await lua_env.manager.target_configs.upsert(
        TargetConversationConfig(target_kind="agent", target_name="echo", multichannel=True)
    )
    person = standing_person("15550003333", "line", target_name="echo")
    second = person.addresses[0].model_copy(update={"channel": "other", "our_identity": "bot", "address": "u-1"})
    await lua_env.seed_person(person.model_copy(update={"addresses": [*person.addresses, second]}))
    channels(FakeChannel())

    await notify_user("hello", channel="twilio", recipient="+15550003333")

    [record] = await send_records(lua_env)
    assert record.thread_id == person_thread_id("15550003333")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL


async def test_notify_inside_a_turn_to_another_participant_lands_in_that_participant_thread(lua_env, channels):
    await _routed(lua_env)
    lead = await _lead(lua_env)
    channels(FakeChannel())
    token = set_conversation_turn(ConversationTurnRef(thread_id=lead.thread_id, message_id="lead-1", route_name="line"))
    try:
        await notify_user("for someone else", channel="twilio", recipient="+15550004444")
    finally:
        reset_conversation_turn(token)

    [record] = await send_records(lua_env)
    assert record.thread_id == route_thread_id("line", "+15550004444")
    assert record.turn_message_id is None


class _HandleChannel(FakeChannel):
    """A channel addressed by a handle before the send and by a numeric id after it."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def resolve_address(self, recipient, sender_identity):
        return super().resolve_address("@handle", sender_identity)

    async def notify(self, notification):
        self.sends.append(notification)
        if self.fail:
            raise ChannelDeliveryError("the medium refused the send", retryable=False)
        return SendOutcome(our_identity=_IDENTITY, address="12345", message_ids=["m-9"])


async def test_the_outcome_address_places_the_record_where_an_inbound_from_it_lands(lua_env, channels):
    from tai42_skeleton.conversations.turn.routing import _resolve_thread_id

    await _routed(lua_env)
    channels(_HandleChannel())

    await notify_user("hello", channel="twilio", recipient="@handle")

    [record] = await send_records(lua_env)
    assert record.client_address == "12345"
    assert record.thread_id == await _resolve_thread_id(_channel_route(), None, "12345")
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, "12345", "m-9") == [record.message_id]
    assert await lua_env.store.resolve_message_ref("twilio", _IDENTITY, "@handle", "m-9") == []


async def test_a_send_that_raises_is_placed_under_its_pre_send_address(lua_env, channels):
    await _routed(lua_env)
    channels(_HandleChannel(fail=True))

    with pytest.raises(ChannelDeliveryError, match="the medium refused the send"):
        await notify_user("hello", channel="twilio", recipient="@handle")

    [record] = await send_records(lua_env)
    assert record.delivery_status is DeliveryStatus.FAILED
    assert record.client_address == "@handle"
    assert record.thread_id == route_thread_id("line", "@handle")
    assert "the medium refused the send" in (record.error or "")


async def test_a_send_with_no_route_goes_out_unrecorded_and_keeps_its_trace_index(
    lua_env, channels, monkeypatch, caplog
):
    channels(FakeChannel())
    indexed: list = []

    async def _index(channel, ids, *, trace_id, span_id):
        indexed.append((channel, ids, trace_id, span_id))

    @contextmanager
    def _span(channel, recipient=None):
        yield SimpleNamespace(id="span-7", update=lambda **kwargs: None)

    monkeypatch.setattr(notify_module, "index_send", _index)
    monkeypatch.setattr(notify_module, "send_span", _span)
    monkeypatch.setattr(notify_module, "active_trace_id", lambda: "trace-1")

    with caplog.at_level(logging.DEBUG, logger=sends_module.logger.name):
        assert await notify_user("hello", channel="twilio", recipient=_ADDRESS) == ["out-1"]

    assert await send_records(lua_env) == []
    assert any("goes out unrecorded" in message for message in caplog.messages)
    assert indexed == [("twilio", ["out-1"], "trace-1", "span-7")]


# -- cancellation ---------------------------------------------------------------


async def _cancel_in_flight(channel: GatedChannel, call) -> None:
    task = asyncio.ensure_future(call())
    await channel.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _assert_cancelled_unknown(lua_env: LuaEnv, record: ConversationRecord, caplog) -> None:
    assert record.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
    assert "cancelled" in (record.error or "")
    assert any(
        record.message_id in message and "twilio" in message and record.client_address in message
        for message in caplog.messages
    )
    assert await sending_ids(lua_env) == []
    assert await lua_env.store.settle_due_sends() == []


async def test_a_cancelled_notify_ends_outcome_unknown_and_the_cancellation_propagates(lua_env, channels, caplog):
    await _routed(lua_env)
    channel = channels(GatedChannel())

    with caplog.at_level(logging.WARNING):
        await _cancel_in_flight(channel, lambda: notify_user("hello", channel="twilio", recipient=_ADDRESS))

    [record] = await send_records(lua_env)
    await _assert_cancelled_unknown(lua_env, record, caplog)


async def _refused_reply(lua_env: LuaEnv) -> ConversationRecord:
    now = time.time()
    reply = ConversationRecord(
        message_id="reply-1",
        route_name="line",
        door="channel",
        thread_id=route_thread_id("line", _ADDRESS),
        client_address=_ADDRESS,
        channel="twilio",
        our_identity=_IDENTITY,
        provider_message_id="inbound-1",
        origin="client",
        inbound_text="hi",
        answer_status="answered",
        answer="an answer the medium refused",
        delivery_status=DeliveryStatus.FAILED,
        error="refused",
        created_at=now,
        updated_at=now,
    )
    await create_record(lua_env.store, reply, route=_channel_route())
    return reply


async def test_a_cancelled_refusal_notice_ends_outcome_unknown_and_leaves_the_reply_unchanged(lua_env, caplog):
    await _routed(lua_env)
    reply = await _refused_reply(lua_env)
    before = await lua_env.store.get_record("reply-1")
    channel = GatedChannel()

    with caplog.at_level(logging.WARNING):
        await _cancel_in_flight(channel, lambda: _notify_refusal(lua_env.store, reply, channel))

    [record] = await send_records(lua_env)
    assert record.sent_by == "notice"
    await _assert_cancelled_unknown(lua_env, record, caplog)
    assert await lua_env.store.get_record("reply-1") == before


class _FailingNoticeChannel(FakeChannel):
    async def notify(self, notification):
        raise ChannelDeliveryError("the medium refused the notice", retryable=False)


@pytest.fixture
def notice_sites(lua_env, monkeypatch):
    """Each notice site, called on ``channel``: the refusal notice, the ask-rejected notice and the
    rejected-inbound notice, each addressed from ``_IDENTITY`` to ``_ADDRESS``."""

    async def _no_event(**kwargs):
        return None

    monkeypatch.setattr(inbound_rejection_module, "_emit_inbound_rejected", _no_event)

    async def refusal(channel):
        await _notify_refusal(lua_env.store, await _refused_reply(lua_env), channel)

    async def participant(channel):
        monkeypatch.setattr(inbound_module, "tai42_app", _FakeApp(channel))
        bridge = InboundBridge(
            channel_id="twilio",
            our_identity=_IDENTITY,
            client_address=_ADDRESS,
            cap_key=_ADDRESS,
            provider_message_id="inbound-2",
            medium_message_id=None,
            reply_to_medium_message_id=None,
            bridge_text="maybe",
        )
        await inbound_module._notify_participant(bridge, "that did not match; try again")

    async def rejected(channel):
        monkeypatch.setattr(inbound_rejection_module, "tai42_app", _FakeApp(channel))
        await inbound_rejection_module.notify_inbound_rejected(
            channel_id="twilio",
            recipient=_ADDRESS,
            sender_identity=_IDENTITY,
            kind="image",
            reason=InboundRejectionReason.UNSUPPORTED_TYPE,
        )

    return {"refusal": refusal, "participant": participant, "rejected": rejected}


_NOTICE_SITES = ["refusal", "participant", "rejected"]


@pytest.mark.parametrize("site", _NOTICE_SITES)
async def test_each_notice_is_a_notice_send_record_in_the_participant_thread(site, lua_env, notice_sites):
    await _routed(lua_env)
    channel = FakeChannel()

    await notice_sites[site](channel)

    [record] = await send_records(lua_env)
    assert record.sent_by == "notice"
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.answer == channel.sends[0].message
    assert [sent.provider_message_ids for sent in record.sends] == [["out-1"]]
    assert record.thread_id == route_thread_id("line", _ADDRESS)


@pytest.mark.parametrize("site", _NOTICE_SITES)
async def test_a_failed_notice_is_recorded_failed_and_not_raised(site, lua_env, notice_sites):
    await _routed(lua_env)

    await notice_sites[site](_FailingNoticeChannel())

    [record] = await send_records(lua_env)
    assert record.sent_by == "notice"
    assert record.delivery_status is DeliveryStatus.FAILED
    assert "the medium refused the notice" in (record.error or "")


async def test_a_refusal_notice_whose_sending_write_fails_is_not_sent_and_logged_at_error(
    lua_env, notice_sites, monkeypatch, caplog
):
    await _routed(lua_env)
    channel = FakeChannel()

    async def _down(*args, **kwargs):
        raise ConnectionError("records redis down")

    monkeypatch.setattr(lua_env.store, "begin_send_record", _down)

    with caplog.at_level(logging.ERROR):
        await notice_sites["refusal"](channel)

    assert channel.sends == []
    assert await send_records(lua_env) == []
    [logged] = [entry for entry in caplog.records if entry.levelno == logging.ERROR and "reply-1" in entry.getMessage()]
    assert logged.exc_info is not None
    assert "records redis down" in str(logged.exc_info[1])


async def test_a_cancelled_facet_send_recorded_ends_outcome_unknown_and_propagates(lua_env, caplog):
    await _routed(lua_env)
    facet = ConversationsFacet(skeleton_app)
    channel = GatedChannel()

    async def _send() -> SendOutcome:
        return await channel.notify(SimpleNamespace(recipient=_ADDRESS, sender_identity=_IDENTITY))

    with caplog.at_level(logging.WARNING):
        await _cancel_in_flight(
            channel,
            lambda: facet.send_recorded(
                channel="twilio",
                our_identity=_IDENTITY,
                client_address=_ADDRESS,
                content=AnswerPart(message="a plugin's own send"),
                send=_send,
            ),
        )

    [record] = await send_records(lua_env)
    assert record.sent_by == "channel"
    await _assert_cancelled_unknown(lua_env, record, caplog)


async def test_with_no_conversations_store_notify_still_sends_and_returns_the_ids(no_store, channels):
    channel = channels(FakeChannel())

    assert await notify_user("hello", channel="twilio", recipient=_ADDRESS) == ["out-1"]

    assert [sent.message for sent in channel.sends] == ["hello"]
    assert no_store == []
