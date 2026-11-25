"""The order of outbound sends to one participant on a channel that confirms delivery by receipt.

A channel declaring ``confirms_delivery_by_receipt`` only ACCEPTS a returned send; the next send
to the same participant waits for the previous one's delivery receipt, bounded by the receipt wait.
Every store here runs its real scripts on ``fakeredis``; the receipts arrive through the platform's
receipt door, as a channel's status webhook delivers them."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import pytest
from tai42_contract.channels import ChannelDeliveryError, SendOutcome
from tai42_contract.conversations import AnswerPart, DeliveryReceipt

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.app.instance import app as skeleton_app
from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_channel as delivery_channel_module
from tai42_skeleton.conversations.ledger import ChannelSendLedger
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus, SentMessage
from tai42_skeleton.conversations.send_scope import current_send_scope, send_scope
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import outcome as outcome_module

from .conftest import LuaEnv, _channel_route, create_record, send_records
from .test_delivery_channel import FakeChannel, _FakeDeliveryApp, _parts_record

_CHANNEL = "twilio"
_IDENTITY = "+15550001111"
_ADDRESS = "+15550002222"
_OTHER_ADDRESS = "+15550003333"
_WAIT = 0.3


class ReceiptChannel(FakeChannel):
    """A channel whose returned send is only accepted; its delivery is reported later by receipt.

    ``sent_at`` stamps the monotonic time of every send, in send order."""

    confirms_delivery_by_receipt = True

    def __init__(self, prefix: str = "w1", **kwargs) -> None:
        self.sent_at: list[float] = []
        super().__init__(prefix, watch=lambda: self.sent_at.append(time.monotonic()), **kwargs)


class FanOutChannel(ReceiptChannel):
    """A receipt channel whose one notification goes out as ``messages`` provider messages.

    Between two of them it holds through the platform's facet for the previous one's receipt.
    ``slow_first`` is the medium's time the first message takes to answer, and ``slow_after_hold``
    the time the message after the first hold takes. ``provider_ids`` lists every provider message
    sent, in send order."""

    def __init__(self, messages: int, *, slow_first: float = 0.0, slow_after_hold: float = 0.0) -> None:
        self.provider_ids: list[str] = []
        self._messages = messages
        self._slow_first = slow_first
        self._slow_after_hold = slow_after_hold
        super().__init__()

    async def notify(self, notification) -> SendOutcome:
        self.sends.append(notification.message)
        ids: list[str] = []
        await asyncio.sleep(self._slow_first)
        for index in range(self._messages):
            if ids:
                await ConversationsFacet(skeleton_app).await_delivery(_CHANNEL, ids[-1])
                if index == 1:
                    await asyncio.sleep(self._slow_after_hold)
            ids.append(f"{self._prefix}-{len(self.sends)}-{index}")
            self.provider_ids.append(ids[-1])
        return self.outcome(notification, ids)


def _bounds(monkeypatch, *, send_timeout: float, receipt_wait: float, lease: int) -> None:
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_SEND_TIMEOUT_SECONDS", str(send_timeout))
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_RECEIPT_WAIT_SECONDS", str(receipt_wait))
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS", str(lease))


@pytest.fixture
def holds_past_the_send_bound(monkeypatch):
    """Holds inside one notify that together outlast the send bound; requested before ``lua_env``."""
    _bounds(monkeypatch, send_timeout=0.5, receipt_wait=0.3, lease=120)


@pytest.fixture
def holds_past_the_lease(monkeypatch):
    """Holds inside one notify that together outlast the claim lease and the settle deadline; before ``lua_env``."""
    _bounds(monkeypatch, send_timeout=0.4, receipt_wait=0.5, lease=1)
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS", "0.5")


@pytest.fixture
def holds_around_the_medium_past_the_lease(monkeypatch):
    """A hold before a send and a hold inside it that, with the medium's time between them, outlast
    the claim lease while one hold and the medium's bound stay under it; requested before ``lua_env``."""
    _bounds(monkeypatch, send_timeout=0.5, receipt_wait=0.4, lease=1)
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS", "0.5")


@pytest.fixture(autouse=True)
def _receipt_settings(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS", "120")
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", f'{{"{_CHANNEL}": 1000}}')
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_RECEIPT_WAIT_SECONDS", str(_WAIT))
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_RECEIPT_POLL_SECONDS", "0.02")


@pytest.fixture(autouse=True)
def _no_grace_timers(monkeypatch):
    """The fallback confirmation a completed send schedules is another mechanism."""

    async def _noop(message_id: str, grace_seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_module, "_confirm_after_grace", _noop)


@pytest.fixture
def wire(monkeypatch) -> Callable[[FakeChannel], FakeChannel]:
    """Answer the delivery machine's and ``notify_user``'s channel lookups with one channel."""

    def _wire(channel: FakeChannel) -> FakeChannel:
        monkeypatch.setattr(delivery_module, "tai42_app", _FakeDeliveryApp(channel))
        monkeypatch.setattr(notify_module, "resolve_channel", lambda name: channel)
        return channel

    return _wire


def _parts(*messages: str) -> list[AnswerPart]:
    return [AnswerPart(message=message) for message in messages]


async def _start_delivery(lua_env: LuaEnv, record: ConversationRecord) -> asyncio.Task[None]:
    """Create and claim ``record``, then drive its channel send in the background."""
    store = lua_env.store
    await create_record(store, record)
    assert await store.claim_delivery(record.message_id, time.time(), "worker-1", 120) == 1
    stored = await store.get_record(record.message_id)
    assert stored is not None
    return asyncio.ensure_future(delivery_channel_module._deliver_channel(store, stored, "worker-1"))


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.005)


async def _receipt(provider_message_id: str, status: DeliveryReceipt = DeliveryReceipt.DELIVERED, errors=None):
    await delivery_module.record_delivery_status(_CHANNEL, provider_message_id, status, errors=errors)


async def _status(lua_env: LuaEnv, message_id: str) -> DeliveryStatus:
    record = await lua_env.store.get_record(message_id)
    assert record is not None
    return record.delivery_status


async def test_each_part_waits_for_the_previous_parts_delivery_receipt(lua_env, wire):
    channel = wire(ReceiptChannel())
    task = await _start_delivery(lua_env, _parts_record("m-order", _parts("first", "second", "third")))

    await _until(lambda: channel.sends == ["first"])
    await asyncio.sleep(0.1)
    assert channel.sends == ["first"]

    await _receipt("w1-1")
    await _until(lambda: channel.sends == ["first", "second"])
    await asyncio.sleep(0.1)
    assert channel.sends == ["first", "second"]

    await _receipt("w1-2")
    await asyncio.wait_for(task, 2)
    assert channel.sends == ["first", "second", "third"]


async def test_a_missing_receipt_releases_the_next_part_after_the_wait_with_one_warning(lua_env, wire, caplog):
    channel = wire(ReceiptChannel())
    with caplog.at_level(logging.WARNING):
        task = await _start_delivery(lua_env, _parts_record("m-late", _parts("first", "second")))
        await asyncio.wait_for(task, 2)

    assert channel.sends == ["first", "second"]
    assert channel.sent_at[1] - channel.sent_at[0] >= _WAIT
    [warning] = [r.getMessage() for r in caplog.records if "no delivery receipt" in r.getMessage()]
    assert "w1-1" in warning
    assert repr(_CHANNEL) in warning
    assert repr(_ADDRESS) in warning
    assert "0.3s" in warning


async def test_a_failed_receipt_for_the_previous_part_stops_the_answer_and_fails_the_record(lua_env, wire, caplog):
    channel = wire(ReceiptChannel())
    with caplog.at_level(logging.ERROR):
        task = await _start_delivery(lua_env, _parts_record("m-failed", _parts("first", "second", "third")))
        await _until(lambda: channel.sends == ["first"])
        await _receipt("w1-1", DeliveryReceipt.FAILED, errors=[{"code": 41}])
        await asyncio.wait_for(task, 2)

    assert channel.sends == ["first", outcome_module._ERROR_ANSWER_TEXT]
    assert await _status(lua_env, "m-failed") is DeliveryStatus.FAILED
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_a_receipt_that_landed_before_the_hold_releases_the_next_part_at_once(lua_env, wire):
    channel = wire(ReceiptChannel())
    # The provider can report delivery before the send's own return reaches the sender.
    await lua_env.store.signal_receipt(_CHANNEL, "w1-1", DeliveryReceipt.DELIVERED)

    task = await _start_delivery(lua_env, _parts_record("m-early", _parts("first", "second")))
    await asyncio.wait_for(task, 2)

    assert channel.sends == ["first", "second"]
    assert channel.sent_at[1] - channel.sent_at[0] < _WAIT / 2


async def test_a_channel_that_declares_nothing_sends_back_to_back_and_writes_no_order_keys(lua_env, wire):
    channel = wire(FakeChannel("w1"))
    task = await _start_delivery(lua_env, _parts_record("m-plain", _parts("first", "second", "third")))
    await asyncio.wait_for(task, 2)

    assert channel.sends == ["first", "second", "third"]
    assert await lua_env.redis.keys("*:receipt:*") == []
    assert await lua_env.redis.keys("*:order:*") == []


async def test_a_notify_user_send_waits_for_the_answer_last_sent_to_the_same_participant(lua_env, wire):
    channel = wire(ReceiptChannel())
    task = await _start_delivery(lua_env, _parts_record("m-cross", _parts("answer")))
    await asyncio.wait_for(task, 2)

    # Another participant of the same channel is not held.
    elsewhere = time.monotonic()
    await notify_user("elsewhere", channel=_CHANNEL, recipient=_OTHER_ADDRESS)
    assert time.monotonic() - elsewhere < _WAIT / 2

    notice = asyncio.ensure_future(notify_user("follow-up", channel=_CHANNEL, recipient=_ADDRESS))
    await asyncio.sleep(0.1)
    assert channel.sends == ["answer", "elsewhere"]

    await _receipt("w1-1")
    assert await asyncio.wait_for(notice, 2) == ["w1-3"]
    assert channel.sends == ["answer", "elsewhere", "follow-up"]


async def test_the_next_part_waits_on_its_own_previous_part_whatever_the_participant_marker_names(lua_env, wire):
    channel = wire(ReceiptChannel())
    store = lua_env.store
    task = await _start_delivery(lua_env, _parts_record("m-own", _parts("first", "second", "third")))
    await _until(lambda: channel.sends == ["first"])
    await _receipt("w1-1")
    await _until(lambda: channel.sends == ["first", "second"])

    async def _marker_names_part_two() -> None:
        while (marker := await store.previous_outbound(_CHANNEL, _IDENTITY, _ADDRESS)) is None or (
            marker.provider_message_id != "w1-2"
        ):
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_marker_names_part_two(), 2)
    # Another sender's message, already delivered, now names the participant's last message.
    await store.set_previous_outbound(_CHANNEL, _IDENTITY, _ADDRESS, "foreign-1", time.time())
    await store.signal_receipt(_CHANNEL, "foreign-1", DeliveryReceipt.DELIVERED)
    await asyncio.sleep(0.1)
    assert channel.sends == ["first", "second"]

    await _receipt("w1-2")
    await asyncio.wait_for(task, 2)
    assert channel.sends == ["first", "second", "third"]


async def test_a_resumed_send_sends_its_next_part_at_once(lua_env, wire):
    channel = wire(ReceiptChannel("w2"))
    # A dead worker sent part one; the ledger names its id.
    await ChannelSendLedger(ConversationsSettings()).append("m-resume", len("first"), ["w1-1"], part=0)

    started = time.monotonic()
    task = await _start_delivery(lua_env, _parts_record("m-resume", _parts("first", "second")))
    await asyncio.wait_for(task, 2)

    assert channel.sends == ["second"]
    assert channel.sent_at[0] - started < _WAIT / 2


async def test_a_resumed_send_stops_the_answer_on_a_failed_receipt_for_its_ledgered_last_part(lua_env, wire, caplog):
    channel = wire(ReceiptChannel("w2"))
    store = lua_env.store
    # A dead worker sent part one; its receipt came back FAILED.
    await ChannelSendLedger(ConversationsSettings()).append("m-resume-failed", len("first"), ["w1-1"], part=0)
    await store.signal_receipt(_CHANNEL, "w1-1", DeliveryReceipt.FAILED, errors=[{"code": 41}])

    with caplog.at_level(logging.ERROR):
        task = await _start_delivery(lua_env, _parts_record("m-resume-failed", _parts("first", "second")))
        await asyncio.wait_for(task, 2)

    assert "second" not in channel.sends
    assert await _status(lua_env, "m-resume-failed") is DeliveryStatus.FAILED
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def _sent_by_a_dead_worker(lua_env: LuaEnv, message_id: str, ids: list[str]) -> None:
    """``message_id`` created and claimed by a worker whose lease has since lapsed, which sent one part per id."""
    store = lua_env.store
    assert await store.claim_delivery(message_id, time.time() - 200, "worker-dead", 120) == 1
    for part, pid in enumerate(ids):
        await ChannelSendLedger(ConversationsSettings()).append(message_id, 5, [pid], part=part)
        assert await store.index_outbound(_CHANNEL, _IDENTITY, _ADDRESS, [pid], message_id) == 1


async def test_a_takeover_stops_the_answer_on_the_failed_receipt_of_its_last_part_long_after_the_receipt_wait(
    lua_env, wire, caplog
):
    channel = wire(ReceiptChannel("w2"))
    store = lua_env.store
    await create_record(store, _parts_record("m-takeover", _parts("first", "second", "third")))
    await _sent_by_a_dead_worker(lua_env, "m-takeover", ["w1-1", "w1-2"])
    with caplog.at_level(logging.ERROR):
        # Part one is delivered and its receipt parked on the record; part two's FAILED conflicts with it.
        await _receipt("w1-1")
        await _receipt("w1-2", DeliveryReceipt.FAILED, errors=[{"code": 41}])
        # The takeover comes after the lease, long past the receipt wait.
        await asyncio.sleep(2 * _WAIT + 0.1)
        [flag] = await store.receipt_flags([(_CHANNEL, "w1-2")])
        assert flag is not None
        assert flag.status is DeliveryReceipt.FAILED

        assert await store.claim_delivery("m-takeover", time.time(), "worker-2", 120) == 1
        stored = await store.get_record("m-takeover")
        assert stored is not None
        await delivery_channel_module._deliver_channel(store, stored, "worker-2")

    assert channel.sends == [outcome_module._ERROR_ANSWER_TEXT]
    assert await _status(lua_env, "m-takeover") is DeliveryStatus.FAILED
    assert any(r.levelno >= logging.ERROR and "m-takeover" in r.getMessage() for r in caplog.records)


def _flag_key(provider_message_id: str) -> str:
    return ConversationsSettings().keyspace.receipt_flag_key(_CHANNEL, provider_message_id)


def _retention_ms(lua_env: LuaEnv) -> int:
    return lua_env.store.settings.answer_retention_ttl_seconds * 1000


async def test_a_receipt_during_the_send_keeps_its_flag_for_the_record_retention(lua_env):
    await create_record(lua_env.store, _parts_record("m-kept", _parts("first", "second")))
    await _sent_by_a_dead_worker(lua_env, "m-kept", ["w1-1"])

    await _receipt("w1-1")
    assert 2 * _WAIT * 1000 < await lua_env.redis.pttl(_flag_key("w1-1")) <= _retention_ms(lua_env)

    # A conflicting receipt keeps its flag too: the record's verdict stands, the message's own does as well.
    assert await lua_env.store.index_outbound(_CHANNEL, _IDENTITY, _ADDRESS, ["w1-2"], "m-kept") == 1
    await _receipt("w1-2", DeliveryReceipt.FAILED, errors=[{"code": 41}])
    assert 2 * _WAIT * 1000 < await lua_env.redis.pttl(_flag_key("w1-2")) <= _retention_ms(lua_env)


async def test_a_receipt_for_a_completed_send_keeps_the_short_flag(lua_env):
    store = lua_env.store
    await create_record(store, _parts_record("m-done", _parts("first")))
    assert await store.claim_delivery("m-done", time.time(), "worker-1", 120) == 1
    assert await store.index_outbound(_CHANNEL, _IDENTITY, _ADDRESS, ["w1-1"], "m-done") == 1
    sent = [SentMessage(part=0, chars=5, provider_message_ids=["w1-1"])]
    assert await store.mark_provisional("m-done", sent, 1, time.time(), "worker-1") == 1

    await _receipt("w1-1")

    assert 0 < await lua_env.redis.pttl(_flag_key("w1-1")) <= 2 * _WAIT * 1000


async def test_indexing_an_id_keeps_the_flag_its_early_receipt_set(lua_env):
    store = lua_env.store
    await create_record(store, _parts_record("m-early-flag", _parts("first", "second")))
    assert await store.claim_delivery("m-early-flag", time.time(), "worker-1", 120) == 1
    # The provider reports delivery before the send's own return reaches the sender.
    await store.signal_receipt(_CHANNEL, "w1-1", DeliveryReceipt.DELIVERED)

    assert await store.index_outbound(_CHANNEL, _IDENTITY, _ADDRESS, ["w1-1", "w1-2"], "m-early-flag") == 1

    assert _retention_ms(lua_env) - 60_000 < await lua_env.redis.pttl(_flag_key("w1-1")) <= _retention_ms(lua_env)
    assert await lua_env.redis.exists(_flag_key("w1-2")) == 0


async def test_the_receipt_door_flags_the_receipt_before_resolving_its_id(lua_env):
    with pytest.raises(LookupError):
        await _receipt("unknown-1", DeliveryReceipt.FAILED, errors={"code": 1})

    [flag] = await lua_env.store.receipt_flags([(_CHANNEL, "unknown-1")])
    assert flag is not None
    assert flag.status is DeliveryReceipt.FAILED
    assert flag.errors == {"code": 1}
    raw = await lua_env.redis.get(ConversationsSettings().keyspace.receipt_flag_key(_CHANNEL, "unknown-1"))
    assert json.loads(raw)["status"] == DeliveryReceipt.FAILED.value


async def test_await_delivery_returns_once_the_message_is_reported_delivered(lua_env):
    await lua_env.store.signal_receipt(_CHANNEL, "p-1", DeliveryReceipt.DELIVERED)
    started = time.monotonic()

    await ConversationsFacet(skeleton_app).await_delivery(_CHANNEL, "p-1")

    assert time.monotonic() - started < _WAIT / 2


async def test_await_delivery_raises_a_non_retryable_error_carrying_the_detail_of_a_failed_receipt(lua_env):
    await lua_env.store.signal_receipt(_CHANNEL, "p-2", DeliveryReceipt.FAILED, errors=[{"code": 42}])

    with pytest.raises(ChannelDeliveryError, match=r"'code': 42") as raised:
        await ConversationsFacet(skeleton_app).await_delivery(_CHANNEL, "p-2")

    assert raised.value.retryable is False


async def test_await_delivery_warns_and_returns_when_no_receipt_arrives_in_time(lua_env, caplog):
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await ConversationsFacet(skeleton_app).await_delivery(_CHANNEL, "p-3")

    assert current_send_scope() is None
    assert time.monotonic() - started >= _WAIT
    [warning] = [r.getMessage() for r in caplog.records if "no delivery receipt" in r.getMessage()]
    assert "p-3" in warning
    assert repr(_CHANNEL) in warning


async def test_a_keepalive_that_raises_stops_the_wait_and_restarts_the_medium_clock():
    async def _refused() -> None:
        raise RuntimeError("the store refused the refresh")

    waited: list[bool] = []
    async with send_scope(0.5, _refused) as clock:
        scope = current_send_scope()
        assert scope is not None
        with pytest.raises(RuntimeError, match="refused the refresh"):
            async with scope.platform_wait():
                waited.append(True)
        assert clock.when() is not None

    assert waited == []


async def test_the_holds_inside_one_notify_do_not_spend_the_sends_medium_bound(
    holds_past_the_send_bound, lua_env, wire, caplog
):
    # Three holds of 0.3s each inside one notify: 0.9s of the platform's waiting against a 0.5s bound.
    channel = wire(FanOutChannel(4))
    with caplog.at_level(logging.WARNING):
        task = await _start_delivery(lua_env, _parts_record("m-fan", _parts("one")))
        await asyncio.wait_for(task, 3)

    record = await lua_env.store.get_record("m-fan")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    [sent] = record.sends
    assert sent.provider_message_ids == channel.provider_ids
    assert len(channel.provider_ids) == 4
    warnings = [r.getMessage() for r in caplog.records if "reported no delivery receipt within 0.3s" in r.getMessage()]
    assert len(warnings) == 3
    assert not any("did not answer within" in r.getMessage() for r in caplog.records)


async def test_the_claim_is_kept_alive_by_every_hold_inside_a_notify(holds_past_the_lease, lua_env, wire):
    # Four holds of 0.5s inside one notify outlast the 1s claim lease taken before the chunk.
    channel = wire(FanOutChannel(5))
    task = await _start_delivery(lua_env, _parts_record("m-lease", _parts("one")))
    await _until(lambda: len(channel.provider_ids) >= 4, timeout=3)

    assert await lua_env.store.claim_delivery("m-lease", time.time(), "worker-2", 1) == 0

    await asyncio.wait_for(task, 3)
    record = await lua_env.store.get_record("m-lease")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert [sent.provider_message_ids for sent in record.sends] == [channel.provider_ids]
    assert len(channel.provider_ids) == 5


async def test_a_later_part_keeps_its_claim_from_the_hold_before_it_through_the_hold_inside_it(
    holds_around_the_medium_past_the_lease, lua_env, wire
):
    # Part two holds 0.4s for part one's receipt, its first message takes 0.4s of the medium's
    # time, then it holds 0.4s inside the notify: 1.2s after its claim against a 1s lease.
    channel = wire(FanOutChannel(2, slow_first=0.4))
    task = await _start_delivery(lua_env, _parts_record("m-later", _parts("one", "two")))
    await _until(lambda: len(channel.provider_ids) >= 3, timeout=3)
    await asyncio.sleep(0.3)

    assert await lua_env.store.claim_delivery("m-later", time.time(), "worker-2", 1) == 0

    await asyncio.wait_for(task, 3)
    record = await lua_env.store.get_record("m-later")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert [sent.provider_message_ids for sent in record.sends] == [channel.provider_ids[:2], channel.provider_ids[2:]]


async def test_a_single_send_with_holds_inside_it_completes_and_is_not_settled_while_live(
    holds_past_the_lease, lua_env, wire
):
    await lua_env.manager.put_route(_channel_route())
    channel = wire(FanOutChannel(5))
    notice = asyncio.ensure_future(notify_user("notice", channel=_CHANNEL, recipient=_ADDRESS))
    await _until(lambda: len(channel.provider_ids) >= 4, timeout=3)
    [sending] = await send_records(lua_env)
    settings = lua_env.store.settings
    # Past the settle deadline the record was begun with; the holds since moved it on.
    begun_deadline = sending.created_at + settings.delivery_send_timeout_seconds + settings.delivery_claim_lease_seconds
    assert time.time() > begun_deadline

    assert await lua_env.store.settle_due_sends() == []
    assert await _status(lua_env, sending.message_id) is DeliveryStatus.SENDING

    assert await asyncio.wait_for(notice, 3) == channel.provider_ids
    assert len(channel.provider_ids) == 5
    assert await _status(lua_env, sending.message_id) is DeliveryStatus.PROVISIONAL


async def test_the_medium_bound_still_cuts_a_provider_that_does_not_answer_after_a_hold_in_a_reply(
    holds_past_the_send_bound, lua_env, wire, caplog
):
    # The hold is not counted; the 0.6s the provider takes after it is the medium's, past its 0.5s bound.
    channel = wire(FanOutChannel(3, slow_after_hold=0.6))
    with caplog.at_level(logging.WARNING):
        task = await _start_delivery(lua_env, _parts_record("m-slow", _parts("one")))
        await asyncio.wait_for(task, 3)

    assert channel.provider_ids == ["w1-1-0"]
    assert await _status(lua_env, "m-slow") is DeliveryStatus.PENDING_DELIVERY
    assert any("did not answer within 0.5s" in r.getMessage() for r in caplog.records)


async def test_the_medium_bound_still_cuts_a_provider_that_does_not_answer_after_a_hold_in_a_single_send(
    holds_past_the_send_bound, lua_env, wire
):
    await lua_env.manager.put_route(_channel_route())
    channel = wire(FanOutChannel(3, slow_after_hold=0.6))

    with pytest.raises(ChannelDeliveryError, match=r"did not answer within 0\.5s") as raised:
        await notify_user("notice", channel=_CHANNEL, recipient=_ADDRESS)

    assert raised.value.retryable is False
    assert channel.provider_ids == ["w1-1-0"]
    [record] = await send_records(lua_env)
    assert record.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
