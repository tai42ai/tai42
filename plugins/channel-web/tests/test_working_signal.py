"""The web channel's working-on-it signal: ``signal_working`` writes one ``chat.working``
frame to the pair's live working stream, which keeps only the latest frame and dies one
frame lifetime after it."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelAddressError

from tai42_channel_web import WebChannel

from .conftest import IDENTITY, VISITOR_ID, FakeRedis

pytestmark = pytest.mark.usefixtures("web_env")

_WORKING_KEY = f"channel:web:working:{IDENTITY}:{VISITOR_ID}"
_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"


def test_web_declares_its_frame_lifetime():
    assert WebChannel.working_signal_expiry_seconds == 10.0


async def test_an_assert_writes_one_active_frame_that_expires(fake_redis: FakeRedis):
    await WebChannel().signal_working(recipient=VISITOR_ID, sender_identity=IDENTITY, active=True)

    assert fake_redis.streams[_WORKING_KEY] == [("1-0", {"active": "1", "ttl": "10.0"})]
    assert fake_redis.ttls[_WORKING_KEY] == 10


async def test_a_clear_replaces_the_retained_frame(fake_redis: FakeRedis):
    channel = WebChannel()
    await channel.signal_working(recipient=VISITOR_ID, sender_identity=IDENTITY, active=True)
    await channel.signal_working(recipient=VISITOR_ID, sender_identity=IDENTITY, active=False)

    # Exactly one retained frame: the latest, so a reconnecting page reads the current state.
    assert fake_redis.streams[_WORKING_KEY] == [("2-0", {"active": "0", "ttl": "10.0"})]
    assert fake_redis.ttls[_WORKING_KEY] == 10


async def test_the_composite_recipient_names_the_same_pair(fake_redis: FakeRedis):
    await WebChannel().signal_working(recipient=f"{IDENTITY}:{VISITOR_ID}", active=True)

    assert [fields for _id, fields in fake_redis.streams[_WORKING_KEY]] == [{"active": "1", "ttl": "10.0"}]


async def test_a_frame_never_enters_the_transcript(fake_redis: FakeRedis):
    await WebChannel().signal_working(
        recipient=VISITOR_ID, sender_identity=IDENTITY, provider_message_id="msg-1", active=True
    )

    assert _TRANSCRIPT_KEY not in fake_redis.streams


async def test_a_recipient_that_names_no_conversation_is_refused(fake_redis: FakeRedis):
    with pytest.raises(ChannelAddressError):
        await WebChannel().signal_working(recipient="no-identity-here", active=True)
    assert fake_redis.streams == {}
