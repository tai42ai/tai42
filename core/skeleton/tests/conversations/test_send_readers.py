"""Send records sit in the thread index beside the participant's own messages, and the readers keep their meaning.

A bounded read counts only records that are not send records, so a run of sends never hides the turns
behind them. A reader that picks where the participant last wrote from skips send records, so a send
to another of the person's addresses never redirects a reply, an operator send or an event. A receipt
for a ``notify_user`` send moves its record and reaches the send's trace. The stores run the real
script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import time

import pytest
from tai42_contract.conversations import (
    ConversationEvent,
    ConversationEventSubmission,
    ConversationRoute,
    DeliveryReceipt,
)

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations.delivery import record_delivery_status
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.conversations.turn.completion_delivery import _resolve_completion_target
from tai42_skeleton.conversations.turn.errors import ThreadAddressUnknownError
from tai42_skeleton.conversations.turn.event_door import _resolve_event_thread
from tai42_skeleton.conversations.turn.overlap import _gather_superseded
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations.conversations.operator_send import _person_target

from .conftest import create_record
from .linked_erase import LINE, LINE_B, channel_address, person
from .test_person_erase_absorbed import world  # noqa: F401  # the linked-erase world fixture
from .test_person_erase_states import states_pg  # noqa: F401  # the states store fixture

_DEPTH = 2
_SENDS = _DEPTH + 1
_P = "p0000001"
_A = "+15550003001"
_B = "+15550003002"
_X = "+15550003003"


@pytest.fixture(autouse=True)
def _shallow_queue(monkeypatch):
    """A thread FIFO depth smaller than the run of sends each test writes."""
    monkeypatch.setenv("CONVERSATIONS_THREAD_QUEUE_DEPTH", str(_DEPTH))


def _inbound(
    message_id: str, route: ConversationRoute, thread_id: str, address: str, created_at: float, **update
) -> ConversationRecord:
    """A participant message at intake (``accepted``) on ``route``, created at ``created_at``."""
    record = ConversationRecord(
        message_id=message_id,
        route_name=route.route_name,
        door="channel",
        thread_id=thread_id,
        client_address=address,
        channel=route.channel,
        our_identity=route.our_identity,
        provider_message_id=f"in-{message_id}",
        origin="client",
        inbound_text=f"text of {message_id}",
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=created_at,
        updated_at=created_at,
    )
    return record.model_copy(update=update)


async def _sends(world, address: str) -> list[str]:  # noqa: F811
    """``_SENDS`` ``notify_user`` sends to ``address``; their record ids."""
    return [await world.notify(address) for _ in range(_SENDS)]


async def _thread_members(world, route: ConversationRoute, thread_id: str) -> list[str]:  # noqa: F811
    return await world.redis.zrange(world.settings.keyspace.thread_index_key(route.route_name, thread_id), 0, -1)


# -- bounded reads count only the records that are not sends ----------------------------------


async def test_accepted_after_returns_the_turns_behind_a_run_of_sends(world):  # noqa: F811
    await world.routes(LINE)
    thread = route_thread_id(LINE.route_name, _X)
    now = time.time()
    lead = _inbound("lead", LINE, thread, _X, now - 100)
    await create_record(world.lua_env.store, lead, route=LINE, intake_token="w")
    sends = await _sends(world, _X)
    for index in (1, 2):
        await create_record(
            world.lua_env.store,
            _inbound(f"f{index}", LINE, thread, _X, now + 100 + index),
            route=LINE,
            intake_token="w",
        )
    assert (await _thread_members(world, LINE, thread))[1 : 1 + _SENDS] == sends

    followers = await world.lua_env.store.accepted_after(LINE.route_name, thread, lead.created_at)

    assert [record.message_id for record in followers] == ["f1", "f2"]


async def test_the_superseded_gather_finds_the_record_behind_a_run_of_newer_sends(world):  # noqa: F811
    await world.routes(LINE)
    store = world.lua_env.store
    thread = route_thread_id(LINE.route_name, _X)
    now = time.time()
    dropped = _inbound("dropped", LINE, thread, _X, now - 100)
    await create_record(store, dropped, route=LINE, intake_token="w")
    lead = _inbound("lead", LINE, thread, _X, now - 90)
    await create_record(store, lead, route=LINE, intake_token="w")
    superseded = dropped.model_copy(update={"delivery_status": DeliveryStatus.SUPERSEDED, "successor_id": "lead"})
    assert await store.supersede_record(superseded) == 1
    sends = await _sends(world, _X)
    assert (await _thread_members(world, LINE, thread))[-_SENDS:] == sends

    carried = await _gather_superseded(store, lead, {"lead"})

    assert [record.message_id for record in carried] == ["dropped"]


# -- where the participant last wrote from ------------------------------------------------------


async def _person_on_two_routes(world) -> str:  # noqa: F811
    """Person ``_P`` with ``_A`` on ``line-b`` and ``_B`` on ``line``: ``_A`` wrote, then sends went to ``_B``.

    Answers the person thread id.
    """
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE_B, _A), channel_address(LINE, _B)))
    thread = person_thread_id(_P)
    await create_record(
        world.lua_env.store,
        _inbound("from-a", LINE_B, thread, _A, time.time() - 100),
        route=LINE_B,
        intake_token="w",
    )
    sends = await _sends(world, _B)
    for message_id in sends:
        sent = await world.lua_env.store.get_record(message_id)
        assert sent is not None
        assert (sent.thread_id, sent.route_name, sent.client_address) == (thread, LINE.route_name, _B)
    return thread


async def _sends_only_person(world) -> str:  # noqa: F811
    """Person ``_P`` with ``_A`` on ``line`` and ``_B`` on ``line-b``, whose thread holds only sends to ``_A``.

    Answers the person thread id.
    """
    await world.routes(LINE, LINE_B)
    await world.seed(person(_P, channel_address(LINE, _A), channel_address(LINE_B, _B)))
    thread = person_thread_id(_P)
    for message_id in await _sends(world, _A):
        sent = await world.lua_env.store.get_record(message_id)
        assert sent is not None
        assert sent.thread_id == thread
    return thread


async def test_a_parked_turns_completion_returns_to_where_the_person_wrote_from(world):  # noqa: F811
    thread = await _person_on_two_routes(world)

    route, address = await _resolve_completion_target(thread)

    assert (route.route_name, address) == (LINE_B.route_name, _A)


async def test_an_operator_send_with_no_address_targets_where_the_person_wrote_from(world):  # noqa: F811
    thread = await _person_on_two_routes(world)

    target = await _person_target(world.lua_env.manager, LINE, thread, _P, None)

    assert target == (LINE_B.route_name, _A)


async def test_an_operator_send_with_no_address_to_a_thread_of_sends_only_asks_for_an_address(world):  # noqa: F811
    thread = await _sends_only_person(world)

    with pytest.raises(BadRequestError) as refused:
        await _person_target(world.lua_env.manager, LINE, thread, _P, None)

    assert str(refused.value) == (
        f"thread {thread!r} holds no record to infer a send address from; pass an explicit address"
    )


def _event(thread_id: str) -> ConversationEventSubmission:
    return ConversationEventSubmission(thread_id=thread_id, event=ConversationEvent(event_id="e-1", kind="ping"))


async def test_an_event_by_thread_id_goes_to_the_address_the_merged_person_wrote_from(world):  # noqa: F811
    await world.routes(LINE)
    await world.seed(person(_P, channel_address(LINE, _A), channel_address(LINE, _B)))
    thread = person_thread_id(_P)
    await create_record(
        world.lua_env.store, _inbound("from-a", LINE, thread, _A, time.time() - 100), route=LINE, intake_token="w"
    )
    for message_id in await _sends(world, _B):
        sent = await world.lua_env.store.get_record(message_id)
        assert sent is not None
        assert (sent.thread_id, sent.client_address) == (thread, _B)

    resolved_thread, address, principal, _multichannel = await _resolve_event_thread(
        world.lua_env.store, LINE, _event(thread), "svc"
    )

    assert (resolved_thread, address, principal) == (thread, _A, None)


async def test_an_event_by_thread_id_enters_a_route_thread_a_send_started(world):  # noqa: F811
    await world.routes(LINE)
    thread = route_thread_id(LINE.route_name, _X)
    assert await _thread_members(world, LINE, thread) == []
    await world.notify(_X)

    resolved_thread, address, _principal, _multichannel = await _resolve_event_thread(
        world.lua_env.store, LINE, _event(thread), "svc"
    )

    assert (resolved_thread, address) == (thread, _X)


async def test_an_event_by_thread_id_to_a_person_thread_of_sends_only_is_refused(world):  # noqa: F811
    thread = await _sends_only_person(world)

    with pytest.raises(ThreadAddressUnknownError) as refused:
        await _resolve_event_thread(world.lua_env.store, LINE, _event(thread), "svc")

    assert str(refused.value) == (
        f"thread {thread} holds no message from the participant to address the event to; submit the event by address"
    )


# -- receipts for a notify_user send ----------------------------------------------------------


@pytest.mark.parametrize(
    ("receipt", "status", "errors"),
    [
        (DeliveryReceipt.DELIVERED, DeliveryStatus.DELIVERED, None),
        (DeliveryReceipt.FAILED, DeliveryStatus.FAILED, [{"code": 30003, "title": "unreachable"}]),
    ],
)
async def test_a_receipt_for_a_notify_send_moves_its_record_and_reaches_its_trace(
    world,  # noqa: F811
    monkeypatch,
    receipt,
    status,
    errors,
):
    await world.routes(LINE)
    message_id = await world.notify(_X)
    sent = await world.lua_env.store.get_record(message_id)
    assert sent is not None
    [provider_id] = sent.sends[0].provider_message_ids
    traced: list[tuple] = []

    async def _trace_receipt(channel, provider_message_id, status, *, errors=None):
        traced.append((channel, provider_message_id, status, errors))
        return True

    monkeypatch.setattr(delivery_module, "record_send_receipt", _trace_receipt)

    await record_delivery_status("twilio", provider_id, receipt, errors=errors)

    received = await world.lua_env.store.get_record(message_id)
    assert received is not None
    assert received.delivery_status is status
    assert traced == [("twilio", provider_id, receipt, errors)]


async def test_a_receipt_for_an_id_in_no_record_raises_and_reaches_no_trace(world, monkeypatch):  # noqa: F811
    await world.routes(LINE)
    traced: list[str] = []

    async def _trace_receipt(channel, provider_message_id, status, *, errors=None):
        traced.append(provider_message_id)
        return True

    monkeypatch.setattr(delivery_module, "record_send_receipt", _trace_receipt)

    with pytest.raises(LookupError, match="maps to no answer record"):
        await record_delivery_status("twilio", "never-sent", DeliveryReceipt.DELIVERED)

    assert traced == []
