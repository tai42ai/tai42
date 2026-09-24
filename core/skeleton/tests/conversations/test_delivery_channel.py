"""Per-message channel-door delivery: the resumable multi-chunk send that keeps a re-drive from
re-sending what a provider already accepted, ordered multi-message (rich part) delivery, the
fan-out cap, and the leased send loop.

A crashed worker is a channel raising something the executor does NOT handle, so the send is
abandoned exactly where the process would have died; the ledger decides WHERE a pick-up resumes."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.conversations import AnswerPart, ConversationRoute, DeliveryReceipt
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_skeleton.conversations import cache as cache_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_channel as delivery_channel_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.ledger import ChannelSendLedger
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import outcome as outcome_module

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

#: Every answer in this module is chunked at this width, so a handful of characters makes
#: a genuinely multi-chunk send.
_CHUNK_CHARS = 10


class WorkerDiedError(RuntimeError):
    """What a channel raises to stand in for the worker vanishing mid-send — not a
    ``ChannelDeliveryError``, so the executor does not turn it into a ``failed`` record."""


class FakeChannel:
    """Records every chunk it is asked to send. ``crash_on`` abandons the send on the nth
    chunk (a dead worker), ``fail_on`` refuses it the way a provider does with a NON-retryable
    ``ChannelDeliveryError`` (a vendor validation rejection / hard 4xx), ``retryable_fail_on``
    raises a RETRYABLE ``ChannelDeliveryError`` (a transport fault), ``input_fail_on`` permanently
    refuses its shape (a ``ChannelInputError``), and ``hang_on`` never returns from it (a send
    still in flight)."""

    def __init__(
        self,
        prefix: str = "out",
        *,
        crash_on: int | None = None,
        fail_on: int | None = None,
        retryable_fail_on: int | None = None,
        input_fail_on: int | None = None,
        hang_on: int | None = None,
        watch=None,
    ) -> None:
        self.sends: list[str] = []
        self._prefix = prefix
        self._crash_on = crash_on
        self._fail_on = fail_on
        self._retryable_fail_on = retryable_fail_on
        self._input_fail_on = input_fail_on
        self._hang_on = hang_on
        self._watch = watch

    async def notify(self, notification) -> list[str]:
        self.sends.append(notification.message)
        if self._watch is not None:
            watched = self._watch()
            if inspect.isawaitable(watched):
                await watched
        if self._crash_on is not None and len(self.sends) == self._crash_on:
            raise WorkerDiedError("the worker died mid-send")
        if self._fail_on is not None and len(self.sends) == self._fail_on:
            raise ChannelDeliveryError("the provider refused the chunk")
        if self._retryable_fail_on is not None and len(self.sends) == self._retryable_fail_on:
            raise ChannelDeliveryError("the medium had a transient fault", retryable=True)
        if self._input_fail_on is not None and len(self.sends) == self._input_fail_on:
            raise ChannelInputError("the provider cannot render the chunk")
        if self._hang_on is not None and len(self.sends) == self._hang_on:
            await asyncio.Event().wait()
        return [f"{self._prefix}-{len(self.sends)}"]


class _FakeChannels:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> FakeChannel:
        return self._channel


class _FakeDeliveryApp:
    def __init__(self, channel: FakeChannel) -> None:
        self.channels = _FakeChannels(channel)


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS", "120")
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", f'{{"twilio": {_CHUNK_CHARS}}}')


@pytest.fixture(autouse=True)
def _no_grace_timers(monkeypatch):
    """The hour-long fallback confirmation a completed channel send schedules is a
    different mechanism; stubbing it keeps these tests to the send itself."""

    async def _noop(message_id: str, grace_seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_module, "_confirm_after_grace", _noop)


@pytest.fixture(autouse=True)
def _drop_leftover_tasks():
    yield
    for task in list(delivery_module._DELIVERY_TASKS):
        task.cancel()
    delivery_module._DELIVERY_TASKS.clear()


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    """One faked redis behind both the record store and the send ledger, so a test reads
    the same keyspace the executor writes through."""
    backing = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(backing))
    monkeypatch.setattr(ledger_module, "client_ctx", make_record_client_ctx(backing))
    return backing


@pytest.fixture
def store(fake: FakeRecordRedis) -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _record(message_id: str, answer: str) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text=f"ask {message_id}",
        answer_status="answered",
        answer=answer,
        created_at=now,
        updated_at=now,
    )


def _parts_record(message_id: str, parts: list[AnswerPart]) -> ConversationRecord:
    """A channel record carrying an ordered multi-message/rich answer — ``answer`` is the
    NON-BLANK part messages joined (a media-only part contributes nothing), ``answer_parts`` the
    parts the delivery machine sends one at a time."""
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text=f"ask {message_id}",
        answer_status="answered",
        answer="\n\n".join(part.message for part in parts if part.message.strip()),
        answer_parts=parts,
        created_at=now,
        updated_at=now,
    )


class MediaFakeChannel:
    """Records the FULL notification of every send (so a test can assert the media it carried)
    and ADVERTISES media support, so the executor's capability guard lets a media part through.
    ``crash_on`` abandons the send on the nth notification (a dead worker), matching
    :class:`FakeChannel` — used to prove a media-only part resumes without re-sending."""

    supports_media_notifications = True

    def __init__(self, prefix: str = "out", *, crash_on: int | None = None) -> None:
        self.notifications: list = []
        self._prefix = prefix
        self._crash_on = crash_on

    async def notify(self, notification) -> list[str]:
        self.notifications.append(notification)
        if self._crash_on is not None and len(self.notifications) == self._crash_on:
            raise WorkerDiedError("the worker died mid-send")
        return [f"{self._prefix}-{len(self.notifications)}"]


def _wire_channel(monkeypatch, channel) -> None:
    monkeypatch.setattr(delivery_module, "tai42_app", _FakeDeliveryApp(channel))


def _channel_route(error_reply_text: str | None = None) -> ConversationRoute:
    return ConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
        error_reply_text=error_reply_text,
    )


class _FakeRouteManager:
    """Resolves the one route the refusal notice looks up; ``route`` is swapped by a test that
    exercises a route-carried ``error_reply_text``."""

    def __init__(self, route: ConversationRoute | None) -> None:
        self.route = route

    async def get_route(self, name: str) -> ConversationRoute | None:
        return self.route


@pytest.fixture(autouse=True)
def route_manager(monkeypatch) -> _FakeRouteManager:
    """Wire the could-not-deliver notice's route lookup to a hermetic in-memory route (no
    ``error_reply_text``, so the notice is the built-in uniform text by default)."""
    manager = _FakeRouteManager(_channel_route())
    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: manager)
    return manager


def _claim(fake: FakeRecordRedis, message_id: str) -> tuple[str, float]:
    """The record's live lease as ``(token, expiry)``; an empty token when it is free."""
    raw = fake._hashes[ConversationsSettings().record_key(message_id)]["claim"]
    if not raw:
        return "", 0.0
    token, expiry = raw.split(":", 1)
    return token, float(expiry)


def _expire_claim(fake: FakeRecordRedis, message_id: str) -> None:
    """Age the record's lease out — what the passage of time does to a dead worker's."""
    key = ConversationsSettings().record_key(message_id)
    token = fake._hashes[key]["claim"].split(":", 1)[0]
    fake._hashes[key]["claim"] = f"{token}:{time.time() - 1}"


async def _drain_spawned(store: ConversationRecordStore) -> None:
    """Await every delivery the sweep spawned."""
    pending = [task for task in delivery_module._DELIVERY_TASKS if not task.done()]
    if pending:
        await asyncio.gather(*pending)


async def _get(store: ConversationRecordStore, message_id: str) -> ConversationRecord:
    record = await store.get_record(message_id)
    assert record is not None
    return record


# -- a partial multi-chunk send resumes instead of re-sending ------------------


async def test_a_partial_send_resumes_at_the_first_unsent_chunk(monkeypatch, fake, store):
    """The duplicate this exists to prevent: a send whose worker died after two of four
    chunks. The re-drive must send chunks three and four ONLY — a human must never be
    texted chunk one or two a second time."""
    answer = "aaaaaaaaaabbbbbbbbbbccccccccccdddddddddd"
    await store.create_record(_record("m-part", answer))

    dying = FakeChannel("w1", crash_on=3)
    _wire_channel(monkeypatch, dying)
    assert await store.claim_delivery("m-part", time.time(), "worker-1", 120) == 1
    with pytest.raises(WorkerDiedError):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-part"), "worker-1")
    assert dying.sends == ["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"]
    assert (await _get(store, "m-part")).delivery_status is DeliveryStatus.PENDING_DELIVERY

    # The dead worker's lease lapses, and the sweep's re-drive picks the record up.
    _expire_claim(fake, "m-part")
    resuming = FakeChannel("w2")
    _wire_channel(monkeypatch, resuming)
    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert resuming.sends == ["cccccccccc", "dddddddddd"]
    record = await _get(store, "m-part")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    # The record names every id both workers produced, in send order.
    assert record.outbound_message_ids == ["w1-1", "w1-2", "w2-1", "w2-2"]


async def test_a_resumed_send_reindexes_what_the_ledger_already_knows(monkeypatch, fake, store):
    """A receipt naming a chunk accepted just before the crash must still resolve to its
    record, even if the crash landed between the ledger append and the reverse index."""
    answer = "aaaaaaaaaabbbbbbbbbb"
    await store.create_record(_record("m-index", answer))
    await ChannelSendLedger(ConversationsSettings()).append("m-index", _CHUNK_CHARS, ["w1-1"])
    assert await store.resolve_outbound("twilio", "w1-1") is None

    _wire_channel(monkeypatch, FakeChannel("w2"))
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-index"), "worker-2")

    assert await store.resolve_outbound("twilio", "w1-1") == "m-index"


async def test_a_send_interrupted_after_its_last_chunk_re_sends_nothing(monkeypatch, fake, store):
    """The whole answer was already out and only the record write was lost. The re-drive
    sends nothing at all and just closes the record."""
    answer = "aaaaaaaaaabbbbbbbbbb"
    await store.create_record(_record("m-tail", answer))
    ledger = ChannelSendLedger(ConversationsSettings())
    await ledger.append("m-tail", _CHUNK_CHARS, ["w1-1"])
    await ledger.append("m-tail", _CHUNK_CHARS, ["w1-2"])

    channel = FakeChannel("w2")
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-tail"), "worker-2")

    assert channel.sends == []
    record = await _get(store, "m-tail")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.outbound_message_ids == ["w1-1", "w1-2"]


async def test_a_completed_send_leaves_no_ledger_behind(monkeypatch, fake, store):
    await store.create_record(_record("m-clean", "aaaaaaaaaabbbbbbbbbb"))
    _wire_channel(monkeypatch, FakeChannel())
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-clean"), "worker-1")

    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-clean") == []


async def test_a_non_retryable_provider_refusal_is_terminal_and_notifies(monkeypatch, fake, store):
    """A refusal is not a crash: the send has no idempotency key to retry under, so the
    record fails loudly and keeps the ids of the chunks the provider did take. A NON-retryable
    mid-sequence ``ChannelDeliveryError`` (a vendor validation rejection / hard 4xx) is the channel
    refusing a reachable participant's reply, so the participant IS told the could-not-deliver
    notice after the record is failed."""
    channel = FakeChannel("w1", fail_on=2)
    await store.create_record(_record("m-refused", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-refused"), "worker-1")

    assert (await _get(store, "m-refused")).delivery_status is DeliveryStatus.FAILED
    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-refused") == []
    assert await store.resolve_outbound("twilio", "w1-1") == "m-refused"
    # Two answer chunks were attempted (the second raised before its ledger write), then the notice.
    assert channel.sends == ["aaaaaaaaaa", "bbbbbbbbbb", outcome_module._ERROR_ANSWER_TEXT]


async def test_a_retryable_provider_refusal_is_terminal_with_no_notice(monkeypatch, fake, store):
    """A RETRYABLE mid-sequence ``ChannelDeliveryError`` (a transport fault, a medium 5xx) means the
    participant could not be reached, so it fails the record with NO could-not-deliver notice — the
    boundary the retryable flag draws, matching the transport-exhaustion path. The record is failed
    at once, so the transient fault never spends the whole attempt budget on a re-send."""
    channel = FakeChannel("w1", retryable_fail_on=2)
    await store.create_record(_record("m-transient", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-transient"), "worker-1")

    assert (await _get(store, "m-transient")).delivery_status is DeliveryStatus.FAILED
    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-transient") == []
    # Only the two answer chunks were attempted; no notice went out.
    assert channel.sends == ["aaaaaaaaaa", "bbbbbbbbbb"]
    assert outcome_module._ERROR_ANSWER_TEXT not in channel.sends


async def test_a_permanent_input_refusal_is_terminal_and_notifies_the_participant(monkeypatch, fake, store):
    """A ``ChannelInputError`` is a permanent refusal of the input's shape — retrying cannot
    succeed, so the record fails terminally (never re-driven), exactly as a delivery refusal
    is, and keeps the ids of the chunks the provider did take. The channel is reachable but
    refused the reply, so the participant is told the uniform could-not-deliver notice AFTER the
    record is failed, and the ledger is cleared."""
    channel = FakeChannel("w1", input_fail_on=2)
    await store.create_record(_record("m-input", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-input"), "worker-1")

    assert (await _get(store, "m-input")).delivery_status is DeliveryStatus.FAILED
    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-input") == []
    assert await store.resolve_outbound("twilio", "w1-1") == "m-input"
    # The last send is the uniform notice — the two answer chunks attempted, then the notice.
    assert channel.sends == ["aaaaaaaaaa", "bbbbbbbbbb", outcome_module._ERROR_ANSWER_TEXT]


async def test_a_refusal_notice_uses_the_route_error_reply_text_when_set(monkeypatch, fake, store, route_manager):
    """The could-not-deliver notice resolves the route's own ``error_reply_text`` when it carries
    one — the SAME uniform text the failed park-completion path delivers — not the built-in
    default."""
    spanish = "Lo sentimos, no pudimos entregar la respuesta."
    route_manager.route = _channel_route(error_reply_text=spanish)
    monkeypatch.setenv("CONVERSATIONS_MAX_OUTBOUND_CHUNKS", "3")
    store = ConversationRecordStore(ConversationsSettings())
    channel = FakeChannel()
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-huge-es", "x" * (10 * _CHUNK_CHARS)))

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-huge-es"), "worker-1")

    assert (await _get(store, "m-huge-es")).delivery_status is DeliveryStatus.FAILED
    assert channel.sends == [spanish]


async def test_a_refusal_notice_that_fails_is_logged_and_leaves_the_record_failed(monkeypatch, fake, caplog):
    """The notice send is best-effort and loud: when the channel refuses it too, the failure is
    logged at ERROR, the record stays terminally ``failed``, and the notice is NEVER retried
    (the channel's ``notify`` is called exactly once — for the notice)."""
    monkeypatch.setenv("CONVERSATIONS_MAX_OUTBOUND_CHUNKS", "3")
    store = ConversationRecordStore(ConversationsSettings())
    # The very first (and only) send — the notice — is refused by the channel.
    channel = FakeChannel(fail_on=1)
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-huge-noticefail", "x" * (10 * _CHUNK_CHARS)))

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.conversations.delivery_channel"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-huge-noticefail"), "worker-1")

    assert (await _get(store, "m-huge-noticefail")).delivery_status is DeliveryStatus.FAILED
    assert len(channel.sends) == 1  # only the notice was attempted, never retried
    assert any(
        "could not deliver the client-safe could-not-deliver notice" in record.message
        and record.levelno == logging.ERROR
        for record in caplog.records
    )


def _unrenderable_record(message_id: str, *, operator_send: bool, caller_principal: str | None) -> ConversationRecord:
    """A channel record carrying a media part a text-only channel cannot render — the refusal
    fixture for the notice-vs-no-notice boundary between an operator hand-send and a turn reply."""
    now = time.time()
    part = AnswerPart(message="pic", media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")])
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="operator",
        operator_send=operator_send,
        caller_principal=caller_principal,
        inbound_text="",
        answer_status="answered",
        answer="pic",
        answer_parts=[part],
        created_at=now,
        updated_at=now,
    )


async def test_an_operator_hand_send_refusal_sends_no_participant_notice(monkeypatch, fake, store):
    """An operator's HAND-injected message (``operator_send``) is not a reply to a participant's
    turn, so a refusal fails the record loudly with NO could-not-deliver notice — the admin
    failed-delivery listing is the signal, exactly as for a transport exhaustion. The uniform
    notice speaks of "your message", which no operator hand-send answers."""
    await store.create_record(_unrenderable_record("m-oper", operator_send=True, caller_principal="op-1"))
    channel = FakeChannel("w1")  # text-only: no supports_media_notifications
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-oper"), "worker-1")

    assert (await _get(store, "m-oper")).delivery_status is DeliveryStatus.FAILED
    assert channel.sends == []


async def test_a_completion_delivered_reply_refusal_notifies_the_participant(monkeypatch, fake, store):
    """A park-completion resumed reply rides ``origin="operator"`` but IS the participant's own
    turn's deferred answer (``operator_send`` is False, the completion principal authorised it), so
    a reachable-channel refusal notifies the participant just as a live turn reply's does."""
    await store.create_record(
        _unrenderable_record("m-cmpl", operator_send=False, caller_principal="system:agent-resume")
    )
    channel = FakeChannel("w1")  # text-only: no supports_media_notifications
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-cmpl"), "worker-1")

    assert (await _get(store, "m-cmpl")).delivery_status is DeliveryStatus.FAILED
    # The media answer never went out; the ONE send is the uniform could-not-deliver notice.
    assert channel.sends == [outcome_module._ERROR_ANSWER_TEXT]


async def test_a_refusal_notice_falls_back_and_logs_when_the_route_lookup_faults(monkeypatch, fake, store, caplog):
    """A route-lookup fault must not block the participant notice: it falls back to the built-in
    uniform text AND is logged at ERROR (never silent)."""

    class _BrokenManager:
        async def get_route(self, name: str):
            raise RuntimeError("route store unavailable")

    monkeypatch.setattr(cache_module, "get_conversations_manager", lambda: _BrokenManager())
    monkeypatch.setenv("CONVERSATIONS_MAX_OUTBOUND_CHUNKS", "3")
    store = ConversationRecordStore(ConversationsSettings())
    channel = FakeChannel("w1")
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-routefault", "x" * (10 * _CHUNK_CHARS)))

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.conversations.delivery_channel"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-routefault"), "worker-1")

    assert (await _get(store, "m-routefault")).delivery_status is DeliveryStatus.FAILED
    assert channel.sends == [outcome_module._ERROR_ANSWER_TEXT]
    assert any("could not resolve route" in r.message and r.levelno == logging.ERROR for r in caplog.records)


async def test_a_ledger_claiming_more_than_the_answer_refuses_loudly(monkeypatch, fake, store):
    """A ledger that cannot describe this answer is corrupt state, not a resume point —
    resuming from it would send the wrong text."""
    await store.create_record(_record("m-bad", "short"))
    await ChannelSendLedger(ConversationsSettings()).append("m-bad", 99, ["w1-1"])
    _wire_channel(monkeypatch, FakeChannel())

    with pytest.raises(RuntimeError, match="claims 99 character"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-bad"), "worker-1")


# -- ordered multi-message (rich part) delivery -------------------------------


async def test_a_multi_part_answer_delivers_each_part_as_its_own_message(monkeypatch, fake, store):
    """An ordered multi-message answer sends each part as its own message, in order, and the
    ledger records the part index of each chunk so a resume tells one part's chunks apart."""
    parts = [AnswerPart(message="first"), AnswerPart(message="second"), AnswerPart(message="third")]
    await store.create_record(_parts_record("m-multi", parts))
    channel = FakeChannel("w1")
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-multi"), "worker-1")

    assert channel.sends == ["first", "second", "third"]
    record = await _get(store, "m-multi")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.outbound_message_ids == ["w1-1", "w1-2", "w1-3"]


async def test_a_multi_part_send_refreshes_the_lease_before_every_part(monkeypatch, fake, store):
    """Every part's send — like every chunk's — goes out under a freshly refreshed lease, so a
    racing sweep can never reclaim the record mid-sequence and double-send a part."""
    parts = [AnswerPart(message="first"), AnswerPart(message="second"), AnswerPart(message="third")]
    await store.create_record(_parts_record("m-lease", parts))
    assert await store.claim_delivery("m-lease", time.time() - 300, "worker-1", 120) == 1

    observed: list[float] = []
    channel = FakeChannel("w1", watch=lambda: observed.append(_claim(fake, "m-lease")[1]))
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-lease"), "worker-1")

    assert len(observed) == 3  # one refresh per part
    assert all(expiry > time.time() for expiry in observed)


async def test_a_multi_part_send_stops_and_fails_mid_sequence_on_a_refusal(monkeypatch, fake, store):
    """A non-retryable provider refusal on part two is terminal STOP-AND-FAIL: part three is never
    sent (order is meaning — message three without two corrupts the answer), the record fails
    loudly, the id of the part that DID land is kept, and the participant is told the uniform
    could-not-deliver notice after the record is failed."""
    parts = [AnswerPart(message="first"), AnswerPart(message="second"), AnswerPart(message="third")]
    await store.create_record(_parts_record("m-stopfail", parts))
    channel = FakeChannel("w1", fail_on=2)
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-stopfail"), "worker-1")

    # Part three never went out; the notice follows the two attempted parts.
    assert channel.sends == ["first", "second", outcome_module._ERROR_ANSWER_TEXT]
    assert (await _get(store, "m-stopfail")).delivery_status is DeliveryStatus.FAILED
    assert await store.resolve_outbound("twilio", "w1-1") == "m-stopfail"


async def test_a_multi_part_send_resumes_at_the_unsent_part(monkeypatch, fake, store):
    """A worker died after part one. The re-drive resumes at part two using the per-part ledger,
    re-sending neither part one nor a partial of it — a human is never texted part one twice."""
    parts = [AnswerPart(message="first"), AnswerPart(message="second"), AnswerPart(message="third")]
    await store.create_record(_parts_record("m-presume", parts))

    # The worker dies on the SECOND send (part 1), after part 0 was ledgered but before part 1
    # is: part 1's chunk is left unledgered so the re-drive re-sends it (a duplicate is the
    # cheap side of a loss), and part 0 is never re-sent.
    dying = FakeChannel("w1", crash_on=2)
    _wire_channel(monkeypatch, dying)
    with pytest.raises(WorkerDiedError):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-presume"), "worker-1")
    assert dying.sends == ["first", "second"]  # part 1 was attempted but not ledgered
    # The ledger names part 0 only, so the resume knows part 1 is where to pick up.
    ledgered = await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-presume")
    assert [(c.part, c.chars) for c in ledgered] == [(0, len("first"))]

    # The dead worker's lease lapses; a new worker picks the record up.
    _expire_claim(fake, "m-presume")
    resuming = FakeChannel("w2")
    _wire_channel(monkeypatch, resuming)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-presume"), "worker-2")

    assert resuming.sends == ["second", "third"]  # part 0 never re-sent
    record = await _get(store, "m-presume")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    # part 0's id (re-indexed from the ledger) then the resumed parts, in order.
    assert record.outbound_message_ids == ["w1-1", "w2-1", "w2-2"]


async def test_a_single_plain_text_answer_is_byte_identical_to_the_old_path(monkeypatch, fake, store):
    """A plain single-message answer (answer_parts=None) degenerates to exactly today's send:
    one part, chunked by width, its chunks ledgered as part 0 (proven by a mid-send crash below,
    since a completed send clears the ledger)."""
    await store.create_record(_record("m-single", "aaaaaaaaaabbbbbbbbbb"))  # 20 chars, 2 chunks at width 10
    channel = FakeChannel("w1", crash_on=2)  # crash after the first chunk is ledgered
    _wire_channel(monkeypatch, channel)
    with pytest.raises(WorkerDiedError):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-single"), "worker-1")

    # The first chunk is ledgered under part 0 — the single-part path is the multi-part loop's
    # degenerate case, byte-for-byte.
    ledgered = await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-single")
    assert [(c.part, c.chars) for c in ledgered] == [(0, _CHUNK_CHARS)]

    _expire_claim(fake, "m-single")  # the dead worker's lease lapses
    resuming = FakeChannel("w2")
    _wire_channel(monkeypatch, resuming)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-single"), "worker-2")
    assert resuming.sends == ["bbbbbbbbbb"]  # resumed at the unsent remainder, part 0 not re-sent
    assert (await _get(store, "m-single")).delivery_status is DeliveryStatus.PROVISIONAL


async def test_a_media_part_is_delivered_with_its_media(monkeypatch, fake, store):
    """A media part rides a ChannelNotification carrying its media — the executor builds the
    notification from the part's content fields, exactly as a single notification does today."""
    # A short message so this module's width-10 chunking does not split it — the media rides
    # the part's one (final, non-blank) chunk.
    part = AnswerPart(message="look", media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")])
    await store.create_record(_parts_record("m-media", [part]))
    channel = MediaFakeChannel("w1")
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-media"), "worker-1")

    assert [n.message for n in channel.notifications] == ["look"]
    assert channel.notifications[0].media is not None
    assert channel.notifications[0].media[0].url == "https://cdn.example/i.png"
    assert (await _get(store, "m-media")).delivery_status is DeliveryStatus.PROVISIONAL


async def test_a_media_part_to_a_text_only_channel_is_refused_terminally(monkeypatch, fake, store):
    """A media part routed to a channel that does not advertise media support can never render,
    so the record fails loudly and terminally with the answer NEVER sent — never re-driven forever.
    The channel is reachable, so the participant is told the uniform could-not-deliver notice."""
    part = AnswerPart(message="pic", media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")])
    await store.create_record(_parts_record("m-nocap", [part]))
    channel = FakeChannel("w1")  # a text-only channel: no supports_media_notifications
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-nocap"), "worker-1")

    # The media answer never went out; the ONE send is the uniform could-not-deliver notice.
    assert channel.sends == [outcome_module._ERROR_ANSWER_TEXT]
    assert (await _get(store, "m-nocap")).delivery_status is DeliveryStatus.FAILED


async def test_a_media_only_part_is_delivered_as_one_text_less_send(monkeypatch, fake, store):
    """A media-only part (blank message carrying media) delivers ONE notification with a blank
    message and the media — a caption-less image — and a ledger entry (chars=0) is still written
    so a resume knows it went out. A completed send clears the ledger."""
    part = AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")])
    await store.create_record(_parts_record("m-mediaonly", [part]))
    channel = MediaFakeChannel("w1")
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-mediaonly"), "worker-1")

    assert [n.message for n in channel.notifications] == [""]  # no text bubble
    assert channel.notifications[0].media is not None
    assert channel.notifications[0].media[0].url == "https://cdn.example/i.png"
    record = await _get(store, "m-mediaonly")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.answer == ""  # an all-media answer joins to the empty string
    assert record.outbound_message_ids == ["w1-1"]
    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-mediaonly") == []


async def test_an_ordered_text_media_text_answer_delivers_three_sends_in_order(monkeypatch, fake, store):
    """An ordered [text, media-only, text] answer sends three messages IN ORDER: the first text,
    then the caption-less media (blank message), then the second text — the media-only part is a
    full message in the sequence, not folded into a neighbour."""
    parts = [
        AnswerPart(message="one"),
        AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")]),
        AnswerPart(message="three"),
    ]
    await store.create_record(_parts_record("m-tmt", parts))
    channel = MediaFakeChannel("w1")
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-tmt"), "worker-1")

    assert [n.message for n in channel.notifications] == ["one", "", "three"]
    assert channel.notifications[0].media is None
    assert channel.notifications[1].media is not None
    assert channel.notifications[1].media[0].url == "https://cdn.example/i.png"
    assert channel.notifications[2].media is None
    record = await _get(store, "m-tmt")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.answer == "one\n\nthree"  # media-only part contributes nothing to the joined text
    assert record.outbound_message_ids == ["w1-1", "w1-2", "w1-3"]


async def test_a_media_only_part_is_not_re_sent_on_resume(monkeypatch, fake, store):
    """A worker died after the media-only part (part 1) was ledgered but before part 2 went out.
    The re-drive resumes at part 2 and re-sends NEITHER the text part 0 NOR the media-only part 1
    — a media-only part's chars=0 ledger entry marks it delivered so a resume never doubles it."""
    parts = [
        AnswerPart(message="one"),
        AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")]),
        AnswerPart(message="three"),
    ]
    await store.create_record(_parts_record("m-mresume", parts))

    # Crash on the THIRD send (part 2), after part 0 and the media-only part 1 are ledgered.
    dying = MediaFakeChannel("w1", crash_on=3)
    _wire_channel(monkeypatch, dying)
    with pytest.raises(WorkerDiedError):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-mresume"), "worker-1")
    assert [n.message for n in dying.notifications] == ["one", "", "three"]
    ledgered = await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-mresume")
    assert [(c.part, c.chars) for c in ledgered] == [(0, len("one")), (1, 0)]  # media-only ledgers chars=0

    _expire_claim(fake, "m-mresume")
    resuming = MediaFakeChannel("w2")
    _wire_channel(monkeypatch, resuming)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-mresume"), "worker-2")

    assert [n.message for n in resuming.notifications] == ["three"]  # part 0 and the media-only not re-sent
    record = await _get(store, "m-mresume")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.outbound_message_ids == ["w1-1", "w1-2", "w2-1"]


async def test_a_whitespace_tail_resume_of_a_text_part_sends_nothing(monkeypatch, fake, store):
    """Regression: a text part ``"aaaaaaaaaa   "`` chunks to ``["aaaaaaaaaa", "   "]``. A crash
    landed after the content chunk was ledgered but before the trailing whitespace chunk's
    ledger-skip, so on resume the whole ``remaining`` is a pure-whitespace tail. It must send
    NOTHING — never a blank ``ChannelNotification("   ")``, which the contract rejects
    (message-non-blank), an uncaught ValidationError that would wedge the record permanently."""
    part = AnswerPart(message="aaaaaaaaaa   ")  # 10 content chars + 3 trailing spaces
    await store.create_record(_parts_record("m-wtail", [part]))
    # The content chunk already went out and was ledgered (part 0, chars=10) pre-crash; the
    # whitespace tail's ledger-skip never happened (the crash beat it).
    await ChannelSendLedger(ConversationsSettings()).append("m-wtail", 10, ["w1-1"], part=0)

    channel = FakeChannel("w2")
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-wtail"), "worker-2")

    assert channel.sends == []  # the whitespace tail is ledger-skip, never a blank send
    record = await _get(store, "m-wtail")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL  # completes cleanly, not wedged
    assert record.outbound_message_ids == ["w1-1"]


async def test_a_whitespace_tail_resume_of_a_media_part_does_not_re_send_media(monkeypatch, fake, store):
    """Regression: a part with BOTH text and media — its media rode the content chunk pre-crash.
    A crash landed after that chunk was ledgered but before the trailing whitespace chunk, so on
    resume ``remaining`` is a pure-whitespace tail. It must NOT be marked ``final`` (which would
    DOUBLE-SEND the media); it stays ledger-skip and nothing goes out."""
    part = AnswerPart(
        message="aaaaaaaaaa   ",
        media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/i.png")],
    )
    await store.create_record(_parts_record("m-wmedia", [part]))
    # The content chunk (carrying the media, final=True) already went out and was ledgered.
    await ChannelSendLedger(ConversationsSettings()).append("m-wmedia", 10, ["w1-1"], part=0)

    channel = MediaFakeChannel("w2")
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-wmedia"), "worker-2")

    assert channel.notifications == []  # the media is never re-sent on the whitespace tail
    record = await _get(store, "m-wmedia")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.outbound_message_ids == ["w1-1"]


async def test_a_ledger_entry_without_a_part_reads_as_part_zero(monkeypatch, fake, store):
    """A ledger entry written before the ``part`` field existed names a single-part answer,
    so it reads as part 0 — a single-part answer whose first chunk was ledgered pre-upgrade
    resumes cleanly at its remainder, never re-sending the pre-upgrade chunk."""
    await store.create_record(_record("m-f5", "aaaaaaaaaabbbbbbbbbb"))  # 20 chars, 2 chunks at width 10
    # A legacy entry: no "part" key at all.
    key = ConversationsSettings().chunk_ledger_key("m-f5")
    fake._lists[key] = [json.dumps({"chars": 10, "outbound_ids": ["w1-1"]})]

    channel = FakeChannel("w2")
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-f5"), "worker-2")

    # Read as part 0, so the resume sends ONLY the remainder — never the pre-upgrade chunk.
    assert channel.sends == ["bbbbbbbbbb"]
    assert (await _get(store, "m-f5")).delivery_status is DeliveryStatus.PROVISIONAL


# -- a long send holds its own lease ------------------------------------------


async def test_every_chunk_goes_out_under_a_live_lease(monkeypatch, fake, store):
    """EVERY chunk — the first one included — must be in flight under a lease this worker
    holds, or the sweep can reclaim the record while a chunk is still going out and both
    workers send it."""
    await store.create_record(_record("m-long", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    # The sender's own lease, taken long enough ago that it has ALREADY lapsed: only the
    # per-chunk refresh can carry it through the send.
    assert await store.claim_delivery("m-long", time.time() - 300, "worker-1", 120) == 1
    assert _claim(fake, "m-long")[1] < time.time()

    observed: list[float] = []
    channel = FakeChannel("w1", watch=lambda: observed.append(_claim(fake, "m-long")[1]))
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-long"), "worker-1")

    assert len(observed) == 3
    assert all(expiry > time.time() for expiry in observed)


async def test_a_chunk_the_provider_never_answers_is_left_unledgered(monkeypatch, fake):
    """A provider call that hangs past the send timeout is INDETERMINATE: the executor
    stops there and does not ledger the chunk, so a re-drive re-sends it (a duplicate is
    the cheap side) instead of skipping text that may never have gone out."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_SEND_TIMEOUT_SECONDS", "0.05")
    store = ConversationRecordStore(ConversationsSettings())
    await store.create_record(_record("m-hang", "aaaaaaaaaabbbbbbbbbbcccccccccc"))

    channel = FakeChannel("w1", hang_on=2)
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-hang"), "worker-1")

    assert channel.sends == ["aaaaaaaaaa", "bbbbbbbbbb"]
    # Only the chunk the provider actually answered for is ledgered, so the resume point
    # sits at the start of the indeterminate chunk.
    ledger = ChannelSendLedger(ConversationsSettings())
    assert [c.chars for c in await ledger.sent_chunks("m-hang")] == [_CHUNK_CHARS]
    # The record is left non-terminal for the sweep to re-drive; nothing was truncated.
    assert (await _get(store, "m-hang")).delivery_status is DeliveryStatus.PENDING_DELIVERY


async def test_a_send_timeout_at_or_above_the_lease_is_refused(monkeypatch):
    """A send bounded at or above the lease can still be in flight after the sweep has
    re-claimed the record, which is the duplicate the lease exists to prevent."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_SEND_TIMEOUT_SECONDS", "120")
    with pytest.raises(ValueError, match="DELIVERY_SEND_TIMEOUT_SECONDS"):
        ConversationsSettings()


async def test_a_send_in_flight_holds_the_record_against_a_racing_sweep(monkeypatch, fake, store):
    """The refreshed lease is what the sweep actually respects: a sweep pass over a record
    somebody is mid-send on claims nothing and sends nothing."""
    await store.create_record(_record("m-race", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    assert await store.claim_delivery("m-race", time.time() - 300, "worker-1", 120) == 1

    hanging = FakeChannel("w1", hang_on=3)
    _wire_channel(monkeypatch, hanging)
    sender = asyncio.create_task(
        delivery_channel_module._deliver_channel(store, await _get(store, "m-race"), "worker-1")
    )
    for _ in range(200):
        if len(hanging.sends) == 3:
            break
        await asyncio.sleep(0.01)
    assert len(hanging.sends) == 3

    sweeper = FakeChannel("sweeper")
    _wire_channel(monkeypatch, sweeper)
    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert sweeper.sends == []
    assert _claim(fake, "m-race")[0] == "worker-1"

    sender.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sender


async def test_a_send_stops_when_it_loses_the_lease_mid_flight(monkeypatch, fake, store):
    """Another worker takes the record over while chunk one is in flight. The sender must
    stop there rather than keep texting a human under an authority it no longer holds."""
    await store.create_record(_record("m-lost", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    assert await store.claim_delivery("m-lost", time.time(), "worker-1", 120) == 1

    async def _taken_over() -> None:
        if len(channel.sends) == 1:
            _expire_claim(fake, "m-lost")
            assert await store.claim_delivery("m-lost", time.time(), "worker-2", 120) == 1

    channel = FakeChannel("w1", watch=_taken_over)
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-lost"), "worker-1")

    assert channel.sends == ["aaaaaaaaaa"]
    assert (await _get(store, "m-lost")).delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert _claim(fake, "m-lost")[0] == "worker-2"
    # The ledger still names the chunk that did go out, so the new holder resumes after it.
    assert [c.chars for c in await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-lost")] == [_CHUNK_CHARS]


async def test_a_worker_that_lost_its_lease_cannot_terminalise_the_record(monkeypatch, fake, store):
    """The stale-terminal clobber: a worker whose lease lapsed and was taken over raises a
    provider refusal LATE. Its ``failed`` write must not overwrite the holder's record, and
    it must not clear the ledger the holder is resuming from."""
    await store.create_record(_record("m-stale", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    assert await store.claim_delivery("m-stale", time.time(), "worker-1", 120) == 1

    async def _taken_over() -> None:
        if len(channel.sends) == 1:
            _expire_claim(fake, "m-stale")
            assert await store.claim_delivery("m-stale", time.time(), "worker-2", 120) == 1

    # The refusal lands on the SAME chunk the takeover happened on, so worker-1 reaches its
    # terminal write holding nothing.
    channel = FakeChannel("w1", fail_on=1, watch=_taken_over)
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-stale"), "worker-1")

    assert (await _get(store, "m-stale")).delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert _claim(fake, "m-stale")[0] == "worker-2"


async def test_a_mid_send_receipt_does_not_truncate_the_answer(monkeypatch, fake, store):
    """A provider posts chunk one's DELIVERED callback while chunks two and three are still
    going out — a sub-second window Twilio routinely hits. The receipt must be refused
    loudly, not settle the record and strand the rest of the answer unsent."""
    await store.create_record(_record("m-early", "aaaaaaaaaabbbbbbbbbbcccccccccc"))
    assert await store.claim_delivery("m-early", time.time(), "worker-1", 120) == 1

    async def _receipt_arrives() -> None:
        # Chunk one is indexed and chunk two is in flight — the window a status callback
        # for chunk one actually lands in.
        if len(channel.sends) == 2:
            with pytest.raises(RuntimeError, match="has not finished"):
                await delivery_module.record_delivery_status("twilio", "w1-1", DeliveryReceipt.DELIVERED)

    channel = FakeChannel("w1", watch=_receipt_arrives)
    _wire_channel(monkeypatch, channel)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-early"), "worker-1")

    assert channel.sends == ["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"]
    record = await _get(store, "m-early")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    # Now that the send IS finished, the same receipt settles it.
    await delivery_module.record_delivery_status("twilio", "w1-1", DeliveryReceipt.DELIVERED)
    assert (await _get(store, "m-early")).delivery_status is DeliveryStatus.DELIVERED


async def test_a_stale_failed_write_cannot_undo_a_completed_send(monkeypatch, fake, store):
    """The other half of the clobber: the answer is fully out and ``provisional``, so a
    late ``failed`` write from a worker that no longer owns the record is refused."""
    await store.create_record(_record("m-done", "aaaaaaaaaabbbbbbbbbb"))
    _wire_channel(monkeypatch, FakeChannel("w2"))
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-done"), "worker-2")
    assert (await _get(store, "m-done")).delivery_status is DeliveryStatus.PROVISIONAL

    assert await store.mark_failed("m-done", 1, time.time(), "worker-1") == -2
    assert (await _get(store, "m-done")).delivery_status is DeliveryStatus.PROVISIONAL


# -- a send that can never complete leaves pending_delivery -------------------


async def test_a_corrupt_ledger_drives_the_record_terminal(monkeypatch, fake, store):
    """A ledger that cannot describe the answer can never resume, so the record must reach
    a terminal state: left pending_delivery it is re-driven every lease expiry forever."""
    await store.create_record(_record("m-bad", "short"))
    await ChannelSendLedger(ConversationsSettings()).append("m-bad", 99, ["w1-1"])
    _wire_channel(monkeypatch, FakeChannel())

    with pytest.raises(RuntimeError, match="claims 99 character"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-bad"), "worker-1")

    record = await _get(store, "m-bad")
    assert record.delivery_status is DeliveryStatus.FAILED
    # The attempt is accounted before the ledger is read, so any later fault is bounded too.
    assert record.attempts == 1
    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-bad") == []


async def test_a_provisional_record_is_not_re_sent_by_a_second_deliver(monkeypatch, fake, store):
    """The sweep/spawn race: a deliver() reaching a record after it went provisional must
    claim nothing and re-send nothing — the ledger is cleared, so a re-send would emit the
    whole answer to a human a second time."""
    channel = FakeChannel("w1")
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-prov2", "aaaaaaaaaabbbbbbbbbb"))
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-prov2"), "worker-1")
    assert channel.sends == ["aaaaaaaaaa", "bbbbbbbbbb"]
    assert (await _get(store, "m-prov2")).delivery_status is DeliveryStatus.PROVISIONAL

    resending = FakeChannel("w2")
    _wire_channel(monkeypatch, resending)
    await delivery_module.deliver("m-prov2")

    assert resending.sends == []
    assert (await _get(store, "m-prov2")).delivery_status is DeliveryStatus.PROVISIONAL


async def test_deliver_refuses_a_record_that_is_not_pending_after_the_claim(monkeypatch, fake, store):
    """Defense in depth over the claim: were the claim ever to admit a record past
    pending_delivery, deliver() refuses loudly rather than re-sending a fully sent answer."""
    channel = FakeChannel()
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-guard", "the answer"))
    await store.mark_provisional("m-guard", ["out-1"], 1, time.time(), "tok")

    async def _admit(self, message_id, now, token, lease):
        return 1

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_delivery", _admit)

    with pytest.raises(RuntimeError, match="not pending_delivery"):
        await delivery_module.deliver("m-guard")
    assert channel.sends == []


async def test_a_transient_ledger_read_error_leaves_the_record_deliverable(monkeypatch, fake, store):
    """A redis blip on the ledger read is NOT a corrupt ledger: it must propagate so the
    sweep re-drives, never terminal-fail a deliverable answer at attempt one."""
    import redis.exceptions

    await store.create_record(_record("m-blip", "aaaaaaaaaabbbbbbbbbb"))
    _wire_channel(monkeypatch, FakeChannel())

    async def _boom(self, message_id):
        raise redis.exceptions.ConnectionError("connection reset")

    monkeypatch.setattr(ledger_module.ChannelSendLedger, "sent_chunks", _boom)

    with pytest.raises(redis.exceptions.ConnectionError):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-blip"), "worker-1")

    record = await _get(store, "m-blip")
    assert record.delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert record.attempts == 1


async def test_an_unparseable_ledger_entry_drives_the_record_terminal(monkeypatch, fake, store):
    """A stored entry that cannot be decoded is a record-shaped fault, not a transient one:
    the record reaches a terminal ``failed`` rather than being re-driven forever."""
    await store.create_record(_record("m-garbled", "aaaaaaaaaabbbbbbbbbb"))
    fake._lists[ConversationsSettings().chunk_ledger_key("m-garbled")] = ["{not valid json"]
    _wire_channel(monkeypatch, FakeChannel())

    with pytest.raises(ledger_module.LedgerInconsistentError, match="unparseable"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-garbled"), "worker-1")

    assert (await _get(store, "m-garbled")).delivery_status is DeliveryStatus.FAILED
    assert await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-garbled") == []


async def test_a_corrupt_ledger_fault_under_a_foreign_lease_spares_the_ledger(monkeypatch, fake, store):
    """The corrupt-ledger fail path honors the foreign-lease guard: a worker whose lease was
    taken over may not clear the ledger the new holder is resuming from."""
    await store.create_record(_record("m-race2", "short"))
    ledger = ChannelSendLedger(ConversationsSettings())
    await ledger.append("m-race2", 99, ["w1-1"])
    # A different worker holds the live lease now.
    assert await store.claim_delivery("m-race2", time.time(), "worker-2", 120) == 1
    _wire_channel(monkeypatch, FakeChannel())

    with pytest.raises(RuntimeError, match="claims 99 character"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-race2"), "worker-1")

    # The terminal write is refused (-3) and the holder's ledger survives.
    assert (await _get(store, "m-race2")).delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert [c.chars for c in await ledger.sent_chunks("m-race2")] == [99]


async def test_a_chunk_is_ledgered_before_it_is_reverse_indexed(monkeypatch, fake, store):
    """The load-bearing write order: a crash between the ledger append and the reverse index
    must leave the chunk LEDGERED, so a re-drive resumes after it and never re-sends it.
    Swapping the two calls leaves the sent chunk unledgered and this test goes red."""
    await store.create_record(_record("m-order", "aaaaaaaaaabbbbbbbbbb"))
    channel = FakeChannel("w1")
    _wire_channel(monkeypatch, channel)
    index_outbound = ConversationRecordStore.index_outbound

    async def _raise_first_time(self, channel_name, outbound_ids, message_id):
        raise RuntimeError("crash between the ledger and the reverse index")

    monkeypatch.setattr(ConversationRecordStore, "index_outbound", _raise_first_time)

    with pytest.raises(RuntimeError, match="crash between the ledger"):
        await delivery_channel_module._deliver_channel(store, await _get(store, "m-order"), "worker-1")

    # The first chunk went out and was ledgered before the reverse index was attempted.
    assert channel.sends == ["aaaaaaaaaa"]
    monkeypatch.setattr(ConversationRecordStore, "index_outbound", index_outbound)
    assert [c.chars for c in await ChannelSendLedger(ConversationsSettings()).sent_chunks("m-order")] == [_CHUNK_CHARS]


async def test_a_whitespace_only_chunk_is_ledgered_and_never_sent(monkeypatch, fake, store):
    """A hard cut can leave a chunk of pure whitespace, which the channel contract refuses.
    It is accounted in the ledger and skipped, so the send still completes."""
    await store.create_record(_record("m-ws", "A" * _CHUNK_CHARS + "\n"))
    channel = FakeChannel("w1")
    _wire_channel(monkeypatch, channel)

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-ws"), "worker-1")

    assert channel.sends == ["A" * _CHUNK_CHARS]
    assert (await _get(store, "m-ws")).delivery_status is DeliveryStatus.PROVISIONAL


async def test_an_answer_over_the_fan_out_cap_is_refused_as_an_error_outcome(monkeypatch, fake):
    """A huge answer would fan one inbound message out into many billable provider sends.
    Past the cap the whole answer is refused with ONE client-safe reply and a failed record,
    never a partial or truncated fan-out."""
    monkeypatch.setenv("CONVERSATIONS_MAX_OUTBOUND_CHUNKS", "3")
    store = ConversationRecordStore(ConversationsSettings())
    channel = FakeChannel()
    _wire_channel(monkeypatch, channel)
    # 100 unbreakable characters at width 10 split into 10 chunks, over the cap of 3.
    await store.create_record(_record("m-huge", "x" * (10 * _CHUNK_CHARS)))

    await delivery_channel_module._deliver_channel(store, await _get(store, "m-huge"), "worker-1")

    record = await _get(store, "m-huge")
    assert record.delivery_status is DeliveryStatus.FAILED
    # Exactly one client-safe notice went out; the answer itself was never fanned out.
    assert channel.sends == [outcome_module._ERROR_ANSWER_TEXT]
    assert record.attempts == 1


async def test_a_partial_send_is_not_refused_when_the_cap_is_lowered_mid_flight(monkeypatch, fake):
    """The fan-out cap is an ADMISSION gate, not retroactive. A send already partway out —
    two chunks ledgered — must COMPLETE even after an operator lowers max_outbound_chunks
    below the full answer's chunk count: a human has seen part of the answer and it cannot
    be un-sent. Reverting the 'only refuse when nothing sent' gate refuses this record and
    turns this test red."""
    monkeypatch.setenv("CONVERSATIONS_MAX_OUTBOUND_CHUNKS", "3")
    store = ConversationRecordStore(ConversationsSettings())
    # 4 chunks at width 10, over the newly-lowered cap of 3.
    answer = "aaaaaaaaaabbbbbbbbbbccccccccccdddddddddd"
    await store.create_record(_record("m-resume", answer))
    ledger = ChannelSendLedger(ConversationsSettings())
    await ledger.append("m-resume", _CHUNK_CHARS, ["w1-1"])
    await ledger.append("m-resume", _CHUNK_CHARS, ["w1-2"])

    resuming = FakeChannel("w2")
    _wire_channel(monkeypatch, resuming)
    await delivery_channel_module._deliver_channel(store, await _get(store, "m-resume"), "worker-1")

    # It resumed and finished the remaining chunks — never the client-safe refusal.
    assert resuming.sends == ["cccccccccc", "dddddddddd"]
    record = await _get(store, "m-resume")
    assert record.delivery_status is DeliveryStatus.PROVISIONAL
    assert record.outbound_message_ids == ["w1-1", "w1-2", "w2-1", "w2-2"]
