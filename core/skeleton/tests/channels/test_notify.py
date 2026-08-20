"""The ``notify_user`` helper: with a named channel, one fire-and-forget send
through that channel (the notification, recipient and all, reaches its ``notify``
verbatim) that returns the medium-assigned outbound ids (an empty set for a channel
that answers ``None``, and for the sink path); with
no channel, the message is recorded to the internal notifications sink. Every failure
propagates loudly (``ChannelDeliveryError``, and the protocol's default-body
``NotImplementedError`` from a channel that cannot notify), and a blank message, an
unknown channel name, or a blank recipient is rejected before any send.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    NOTIFICATION_ADDRESS_MAX_CHARS,
    NOTIFICATION_MESSAGE_MAX_CHARS,
    ChannelDeliveryError,
    ChannelNotification,
    ChannelTemplate,
)
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_skeleton.app.instance import app
from tai42_skeleton.channels import notifications_sink
from tai42_skeleton.channels.notify import notify_user


class RecordingChannel:
    """Records every notification handed to ``notify``. Its ``notify`` returns
    ``None`` — a channel not yet on the id-returning contract."""

    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def notify(self, notification: ChannelNotification) -> None:
        self.notifications.append(notification)


class IdReturningChannel:
    """Records every notification and returns the medium-assigned outbound ids — a
    channel on the id-returning ``notify`` contract."""

    def __init__(self, ids: list[str]) -> None:
        self.notifications: list[ChannelNotification] = []
        self._ids = ids

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return self._ids


class RichChannel:
    """A channel advertising ALL richer-notify capabilities via the OPTIONAL
    class-attribute convention; records every notification it receives."""

    supports_media_notifications = True
    supports_template_notifications = True
    supports_interactive_notifications = True

    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return []


class FailingChannel:
    async def notify(self, notification: ChannelNotification) -> None:
        raise ChannelDeliveryError("provider unreachable")


class CannotNotifyChannel:
    """A channel that cannot notify — its ``notify`` raises exactly what the
    contract protocol's default body raises."""

    async def notify(self, notification: ChannelNotification) -> None:
        raise NotImplementedError


@pytest.fixture
def register_channel():
    """Yield a registrar that installs a channel under a name; the registry is
    reset around every test."""
    app._channel_registry.reset()

    def _register(name, channel):
        tai42_app.channels.register(name, channel)
        return channel

    yield _register
    app._channel_registry.reset()


# -- the send -------------------------------------------------------------------


async def test_notify_sends_through_the_named_channel(register_channel):
    channel = register_channel("fake", RecordingChannel())

    await notify_user("Deploy finished", channel="fake")

    assert channel.notifications == [ChannelNotification(message="Deploy finished", recipient=None)]


async def test_notify_forwards_recipient_verbatim(register_channel):
    channel = register_channel("fake", RecordingChannel())

    await notify_user("Ping", channel="fake", recipient="123456")

    assert channel.notifications == [ChannelNotification(message="Ping", recipient="123456")]


async def test_notify_sends_from_the_channels_own_identity(register_channel):
    # The helper builds a notification with no sender_identity, so the channel sends
    # from its configured identity.
    channel = register_channel("fake", RecordingChannel())

    await notify_user("Ping", channel="fake", recipient="123")

    assert channel.notifications == [ChannelNotification(message="Ping", recipient="123")]
    assert channel.notifications[0].sender_identity is None


async def test_notify_returns_channel_outbound_ids(register_channel):
    # A channel on the id-returning contract: the helper surfaces the medium-assigned
    # ids (one per split part) so a later delivery receipt can be correlated back.
    register_channel("ids", IdReturningChannel(["m1", "m2"]))

    assert await notify_user("split message", channel="ids") == ["m1", "m2"]


async def test_notify_none_return_becomes_empty_id_list(register_channel):
    # A channel whose ``notify`` returns ``None`` yields an EMPTY id set: an accepted
    # send with no correlatable id, never an error.
    register_channel("legacy", RecordingChannel())

    assert await notify_user("hi", channel="legacy") == []


# -- media & template: threaded to a capable channel, guarded everywhere else -----


_IMAGE = MediaItem(kind=MediaKind.IMAGE, url="https://example.com/photo.png", caption="a photo")
_TEMPLATE = ChannelTemplate(name="status_update", language="en_US", parameters=["A-42"])


async def test_notify_threads_media_to_a_capable_channel(register_channel):
    channel = register_channel("rich", RichChannel())

    await notify_user("here it is", channel="rich", media=[_IMAGE])

    assert channel.notifications == [ChannelNotification(message="here it is", media=[_IMAGE])]


async def test_notify_threads_template_to_a_capable_channel(register_channel):
    channel = register_channel("rich", RichChannel())

    await notify_user("your item is done", channel="rich", template=_TEMPLATE)

    assert channel.notifications == [ChannelNotification(message="your item is done", template=_TEMPLATE)]


async def test_media_to_channel_without_capability_is_not_implemented(register_channel):
    # A channel that does not advertise supports_media_notifications refuses the media
    # send loudly (NotImplementedError → 501) — never a silent freeform downgrade.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support media notifications"):
        await notify_user("photo", channel="plain", media=[_IMAGE])
    assert channel.notifications == []


async def test_template_to_channel_without_capability_is_not_implemented(register_channel):
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support template notifications"):
        await notify_user("hi", channel="plain", template=_TEMPLATE)
    assert channel.notifications == []


async def test_notify_threads_options_to_a_capable_channel(register_channel):
    channel = register_channel("rich", RichChannel())

    await notify_user("pick one", channel="rich", options=["Item A", "Item B"])

    assert channel.notifications == [ChannelNotification(message="pick one", options=["Item A", "Item B"])]


async def test_notify_threads_options_with_media_to_a_capable_channel(register_channel):
    channel = register_channel("rich", RichChannel())

    await notify_user("a card with a list", channel="rich", media=[_IMAGE], options=["Item A"])

    assert channel.notifications == [
        ChannelNotification(message="a card with a list", media=[_IMAGE], options=["Item A"])
    ]


async def test_options_to_channel_without_capability_is_not_implemented(register_channel):
    # A channel that does not advertise supports_interactive_notifications refuses the
    # options send loudly (NotImplementedError → 501) — never a silent text downgrade.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support interactive notifications"):
        await notify_user("pick one", channel="plain", options=["Item A", "Item B"])
    assert channel.notifications == []


async def test_options_capability_guard_fires_before_the_feed_record(register_channel, sink_redis):
    # A refused options send leaves NO phantom feed entry: the guard precedes the audience
    # in-app record, so an audience-addressed options send to an incapable channel writes
    # nothing.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support interactive notifications"):
        await notify_user("pick one", channel="plain", options=["Item A"], audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_options_and_template_mutual_exclusion_leaves_no_phantom_feed_entry(register_channel, sink_redis):
    # options + template both set is refused by the contract's exclusion validator (a
    # pydantic ValidationError → 400), BEFORE the audience feed record — even on a channel
    # that advertises both capabilities (so the capability guards pass).
    channel = register_channel("rich", RichChannel())

    with pytest.raises(ValidationError, match="mutually exclusive"):
        await notify_user("x", channel="rich", options=["Item A"], template=_TEMPLATE, audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_capability_guard_fires_before_the_feed_record(register_channel, sink_redis):
    # A refused rich send must leave NO phantom feed entry: the guard precedes the
    # audience in-app record, so an audience-addressed media send to an incapable
    # channel writes nothing.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support media notifications"):
        await notify_user("photo", channel="plain", media=[_IMAGE], audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_capability_guard_fires_before_the_feed_record_template(register_channel, sink_redis):
    # Symmetric to the media case: a refused TEMPLATE send to an incapable channel
    # (NotImplementedError → 501) precedes the audience in-app record, so an
    # audience-addressed template send writes NO phantom feed entry.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support template notifications"):
        await notify_user("hi", channel="plain", template=_TEMPLATE, audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_mutual_exclusion_refusal_leaves_no_phantom_feed_entry(register_channel, sink_redis):
    # media + template both set is refused by the contract's mutual-exclusion validator
    # (a pydantic ValidationError → 400). The notification is constructed/validated BEFORE
    # the audience feed record, so an audience-addressed refusal writes no phantom entry —
    # even on a channel that advertises BOTH capabilities (so the capability guards pass).
    channel = register_channel("rich", RichChannel())

    with pytest.raises(ValidationError, match="mutually exclusive"):
        await notify_user("x", channel="rich", media=[_IMAGE], template=_TEMPLATE, audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_empty_media_list_refusal_leaves_no_phantom_feed_entry(register_channel, sink_redis):
    # A present-but-empty media list is refused by the contract's _check_media validator
    # (a pydantic ValidationError → 400). Validation precedes the audience feed record, so an
    # audience-addressed refusal writes no phantom entry. The channel advertises media, so the
    # capability guard passes and the empty-list validator is the refusal reached.
    channel = register_channel("rich", RichChannel())

    with pytest.raises(ValidationError, match="media must be a non-empty list"):
        await notify_user("x", channel="rich", media=[], audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_media_with_channel_none_rejected(sink_redis):
    # The internal sink stores no rich content, so media with no named channel is a
    # loud client error (→ 400), never a silent drop.
    with pytest.raises(ValueError, match="media, template and options require a named channel"):
        await notify_user("photo", media=[_IMAGE])
    assert await notifications_sink.read_notifications() == []


async def test_template_with_channel_none_rejected(sink_redis):
    with pytest.raises(ValueError, match="media, template and options require a named channel"):
        await notify_user("hi", template=_TEMPLATE)
    assert await notifications_sink.read_notifications() == []


async def test_options_with_channel_none_rejected(sink_redis):
    with pytest.raises(ValueError, match="media, template and options require a named channel"):
        await notify_user("pick one", options=["Item A", "Item B"])
    assert await notifications_sink.read_notifications() == []


# -- loud failures: every error propagates, nothing is swallowed -----------------


async def test_delivery_failure_propagates(register_channel):
    register_channel("boom", FailingChannel())

    with pytest.raises(ChannelDeliveryError, match="provider unreachable"):
        await notify_user("hello", channel="boom")


async def test_cannot_notify_channel_surfaces_not_implemented(register_channel):
    # The contract protocol's default ``notify`` body raises NotImplementedError;
    # a channel that cannot notify surfaces exactly that — present and loud,
    # never a silent no-op.
    register_channel("mute", CannotNotifyChannel())

    with pytest.raises(NotImplementedError):
        await notify_user("hello", channel="mute")


# -- loud validation: rejected before any send -----------------------------------


@pytest.fixture
def sink_redis(monkeypatch, fake_redis):
    """Point the internal sink's Redis at the shared fake so ``channel=None``
    writes land somewhere readable back in the test, and configure the interactions
    store so the feed write's OFF guard passes (only the presence gate reads the env;
    the fake connection still stands in)."""
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notifications_sink, "client_ctx", _ctx)
    return fake_redis


async def test_channel_none_records_to_sink(register_channel, sink_redis):
    channel = register_channel("fake", RecordingChannel())

    await notify_user("build passed")

    # It routes to the internal sink, not to any registered channel.
    assert channel.notifications == []
    records = await notifications_sink.read_notifications()
    assert len(records) == 1
    assert records[0]["message"] == "build passed"
    assert records[0]["recipient"] is None
    assert records[0]["id"]
    assert records[0]["created_at"]


async def test_channel_none_stores_recipient(sink_redis):
    await notify_user("ping", recipient="ops")

    records = await notifications_sink.read_notifications()
    assert records[0]["recipient"] == "ops"


async def test_channel_none_returns_empty_id_list(sink_redis):
    # The internal-sink path records an in-app entry that has no medium-assigned id,
    # so the helper returns an empty id set.
    assert await notify_user("recorded") == []


@pytest.mark.parametrize("bad_recipient", ["", "   "])
async def test_channel_none_blank_recipient_rejected(sink_redis, bad_recipient):
    with pytest.raises(ValueError, match="recipient must be a non-empty address"):
        await notify_user("hello", recipient=bad_recipient)
    assert await notifications_sink.read_notifications() == []


async def test_channel_none_message_at_cap_records(sink_redis):
    # The sink stores the raw message, so the notification's message cap is enforced on
    # this path too: a message exactly at the cap is accepted and recorded.
    at_cap = "x" * NOTIFICATION_MESSAGE_MAX_CHARS

    await notify_user(at_cap)

    records = await notifications_sink.read_notifications()
    assert len(records) == 1
    assert records[0]["message"] == at_cap


async def test_channel_none_over_cap_message_rejected(sink_redis):
    # One character past the cap is refused loudly before the write — nothing is recorded,
    # never an unbounded message in the replayed feed.
    over_cap = "x" * (NOTIFICATION_MESSAGE_MAX_CHARS + 1)

    with pytest.raises(ValueError, match="message must be at most"):
        await notify_user(over_cap)
    assert await notifications_sink.read_notifications() == []


# -- recipient & audience length caps: bounded on both paths, both fields --------


async def test_channel_none_recipient_at_cap_records(sink_redis):
    # The sink stores the recipient verbatim, so its address cap is enforced on this
    # path too: an address exactly at the cap is accepted and recorded.
    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS

    await notify_user("hi", recipient=at_cap)

    records = await notifications_sink.read_notifications()
    assert records[0]["recipient"] == at_cap


async def test_channel_none_over_cap_recipient_rejected(sink_redis):
    # One character past the cap is refused before the write — never an unbounded
    # address in the replayed feed record.
    over_cap = "x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1)

    with pytest.raises(ValueError, match="recipient must be at most"):
        await notify_user("hi", recipient=over_cap)
    assert await notifications_sink.read_notifications() == []


async def test_channel_none_audience_at_cap_records(sink_redis):
    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS

    await notify_user("hi", audience=at_cap)

    own = await notifications_sink.read_notifications(audience=at_cap)
    assert len(own) == 1
    assert own[0]["audience"] == at_cap


async def test_channel_none_over_cap_audience_rejected(sink_redis):
    # An over-cap audience is refused before the write — never an oversized per-identity
    # Redis key.
    over_cap = "x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1)

    with pytest.raises(ValueError, match="audience must be at most"):
        await notify_user("hi", audience=over_cap)
    assert await notifications_sink.read_notifications() == []


async def test_channel_recipient_at_cap_forwarded(register_channel):
    channel = register_channel("fake", RecordingChannel())
    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS

    await notify_user("hi", channel="fake", recipient=at_cap)

    assert channel.notifications == [ChannelNotification(message="hi", recipient=at_cap)]


async def test_channel_over_cap_recipient_rejected(register_channel):
    # On the channel path the recipient rides the ChannelNotification, so the contract's
    # address cap refuses it (a ValidationError → 400) before the channel is touched.
    channel = register_channel("fake", RecordingChannel())
    over_cap = "x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1)

    with pytest.raises(ValidationError, match="address must be at most"):
        await notify_user("hi", channel="fake", recipient=over_cap)
    assert channel.notifications == []


async def test_channel_audience_at_cap_records_and_sends(register_channel, sink_redis):
    channel = register_channel("fake", RecordingChannel())
    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS

    await notify_user("hi", channel="fake", audience=at_cap)

    assert channel.notifications == [ChannelNotification(message="hi", recipient=None)]
    own = await notifications_sink.read_notifications(audience=at_cap)
    assert len(own) == 1
    assert own[0]["audience"] == at_cap


async def test_channel_over_cap_audience_rejected(register_channel, sink_redis):
    # The audience cap fires at the top, before the channel is resolved: an over-cap
    # audience touches neither the channel nor the feed.
    channel = register_channel("fake", RecordingChannel())
    over_cap = "x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1)

    with pytest.raises(ValueError, match="audience must be at most"):
        await notify_user("hi", channel="fake", audience=over_cap)
    assert channel.notifications == []
    assert await notifications_sink.read_notifications() == []


# -- audience: the in-app identity axis, honored even on the channel path --------


async def test_channel_with_audience_records_in_app_and_sends(register_channel, sink_redis):
    # The two axes coexist: the channel delivers to ``recipient`` (an address) AND
    # the record lands in ``audience``'s in-app feed (an identity).
    channel = register_channel("fake", RecordingChannel())

    await notify_user("done", channel="fake", recipient="123", audience="alice")

    assert channel.notifications == [ChannelNotification(message="done", recipient="123")]
    own = await notifications_sink.read_notifications(audience="alice")
    assert len(own) == 1
    assert own[0]["message"] == "done"
    assert own[0]["audience"] == "alice"
    assert own[0]["recipient"] == "123"


async def test_channel_without_audience_stores_nothing(register_channel, sink_redis):
    # A plain channel send with no audience records nothing — today's behavior.
    register_channel("fake", RecordingChannel())

    await notify_user("plain", channel="fake")

    assert await notifications_sink.read_notifications() == []


async def test_channel_none_with_audience_writes_the_per_identity_feed(sink_redis):
    await notify_user("hi", audience="alice")

    own = await notifications_sink.read_notifications(audience="alice")
    assert len(own) == 1
    assert own[0]["audience"] == "alice"
    # It is also on the shared feed (operators still see it).
    assert len(await notifications_sink.read_notifications()) == 1


@pytest.mark.parametrize("bad_audience", ["", "   "])
async def test_blank_audience_rejected(sink_redis, bad_audience):
    with pytest.raises(ValueError, match="audience must be a non-empty identity"):
        await notify_user("hello", audience=bad_audience)
    assert await notifications_sink.read_notifications() == []


async def test_unknown_channel_rejected(register_channel):
    with pytest.raises(ValueError, match="unknown channel: 'nope'"):
        await notify_user("hello", channel="nope")


async def test_empty_channel_name_rejected(register_channel):
    with pytest.raises(ValueError, match="channel must be a non-empty string"):
        await notify_user("hello", channel="")


@pytest.mark.parametrize("bad_message", ["", "   ", "\n\t"])
async def test_blank_message_rejected(register_channel, bad_message):
    channel = register_channel("fake", RecordingChannel())

    with pytest.raises(ValueError, match="message must be a non-blank string"):
        await notify_user(bad_message, channel="fake")
    assert channel.notifications == []


async def test_non_string_message_rejected(register_channel):
    channel = register_channel("fake", RecordingChannel())

    with pytest.raises(ValueError, match="message must be a non-blank string"):
        await notify_user(42, channel="fake")  # type: ignore[arg-type]
    assert channel.notifications == []
