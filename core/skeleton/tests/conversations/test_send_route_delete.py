"""A send record whose route is deleted while the send goes out stands, in no thread index.

The send blocks on an event while ``delete_conversation_route`` runs, then is released. Each end
(a completion, a raise, the sweep's settle of a crashed row) keeps the record, flags it
``route_missing`` and writes neither the thread index nor ``route_threads``, which a route delete
has already reclaimed and nothing would reclaim again. With the route standing, each end writes
both. The stores run the real script text on ``fakeredis[lua]``.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.conversations import DeliveryReceipt

from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import sends as sends_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.thread_ids import route_thread_id
from tai42_skeleton.operations import conversations as ops

from .conftest import (
    FakeChannel,
    GatedChannel,
    LuaEnv,
    RaisingGatedChannel,
    _channel_route,
    past_deadline,
    send_records,
)

_ADDRESS = "+15550002222"
_THREAD = route_thread_id("line", _ADDRESS)


@pytest.fixture(autouse=True)
def _no_grace_confirms(monkeypatch):
    monkeypatch.setattr(delivery_module, "_spawn", lambda coro: coro.close())


def _install[C: FakeChannel](monkeypatch, channel: C) -> C:
    monkeypatch.setattr(notify_module, "resolve_channel", lambda name: channel)
    return channel


async def _send(lua_env: LuaEnv, monkeypatch, end: str, *, delete: bool) -> ConversationRecord:
    """Run one ``notify_user`` ending as ``end``, deleting the route while it is going out when ``delete``."""
    if end == "crash":

        async def _stopped(*args, **kwargs):
            return None

        monkeypatch.setattr(sends_module, "_complete", _stopped)
        _install(monkeypatch, FakeChannel())
        await notify_user("hello", channel="twilio", recipient=_ADDRESS)
        [record] = await send_records(lua_env)
        if delete:
            await ops.delete_conversation_route("line")
        await past_deadline(lua_env, record.message_id)
        await delivery_module.sweep_stalled_deliveries()
    else:
        channel = _install(monkeypatch, GatedChannel() if end == "ok" else RaisingGatedChannel())
        task = asyncio.ensure_future(notify_user("hello", channel="twilio", recipient=_ADDRESS))
        await channel.entered.wait()
        if delete:
            await ops.delete_conversation_route("line")
        channel.release.set()
        if end == "ok":
            assert await task == ["out-1"]
        else:
            with pytest.raises(ChannelDeliveryError, match="the medium refused the send"):
                await task
    [record] = await send_records(lua_env)
    return record


_ENDS = [
    ("ok", DeliveryStatus.PROVISIONAL),
    ("raise", DeliveryStatus.FAILED),
    ("crash", DeliveryStatus.OUTCOME_UNKNOWN),
]


@pytest.mark.parametrize(("end", "status"), _ENDS)
async def test_a_send_ending_after_its_route_was_deleted_stands_in_no_thread_index(
    end, status, lua_env, monkeypatch, caplog
):
    await lua_env.manager.put_route(_channel_route())

    with caplog.at_level(logging.WARNING):
        record = await _send(lua_env, monkeypatch, end, delete=True)

    settings = lua_env.store.settings
    assert record.delivery_status is status
    assert record.route_missing is True
    assert await lua_env.redis.exists(settings.keyspace.thread_index_key("line", _THREAD)) == 0
    assert await lua_env.redis.exists(settings.keyspace.route_threads_key("line")) == 0
    assert any(
        record.message_id in message and "line" in message and "deleted" in message for message in caplog.messages
    )


async def test_a_receipt_still_settles_a_send_whose_route_was_deleted(lua_env, monkeypatch):
    await lua_env.manager.put_route(_channel_route())
    record = await _send(lua_env, monkeypatch, "ok", delete=True)

    assert await lua_env.store.resolve_outbound("twilio", "out-1") == record.message_id
    assert await lua_env.store.ingest_receipt(record.message_id, DeliveryReceipt.DELIVERED, time.time()) == 1
    settled = await lua_env.store.get_record(record.message_id)
    assert settled is not None
    assert settled.delivery_status is DeliveryStatus.DELIVERED


@pytest.mark.parametrize(("end", "status"), _ENDS)
async def test_a_send_ending_with_its_route_standing_writes_both_thread_indexes(end, status, lua_env, monkeypatch):
    await lua_env.manager.put_route(_channel_route())

    record = await _send(lua_env, monkeypatch, end, delete=False)

    settings = lua_env.store.settings
    assert record.delivery_status is status
    assert record.route_missing is False
    assert await lua_env.redis.zrange(settings.keyspace.thread_index_key("line", _THREAD), 0, -1) == [record.message_id]
    assert await lua_env.redis.zrange(settings.keyspace.route_threads_key("line"), 0, -1) == [_THREAD]
