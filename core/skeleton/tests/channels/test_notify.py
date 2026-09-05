"""The ``notify_user`` helper: with a named channel, one fire-and-forget send
through that channel (the notification, recipient and all, reaches its ``notify``
verbatim) that returns the medium-assigned outbound ids (an empty set for a channel
that answers ``None``, and for the sink path); with
no channel, the message is recorded to the internal notifications sink. Every failure
propagates loudly (``ChannelDeliveryError``, and the protocol's default-body
``NotImplementedError`` from a channel that cannot notify), and a blank message, an
unknown channel name, or a blank recipient is rejected before any send (a blank
message is admissible only when media carries the content — a media-only send).
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import cast

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.channels import (
    NOTIFICATION_ADDRESS_MAX_CHARS,
    NOTIFICATION_MESSAGE_MAX_CHARS,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
    ChannelTemplate,
    ReplyOption,
)
from tai42_contract.interactions import MEDIA_ROUTE_PREFIX
from tai42_contract.interactions.models import MediaItem, MediaKind

from tai42_skeleton.app.instance import app
from tai42_skeleton.channels import notifications_sink
from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.interactions import media as media_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring

from .._fakes.recording_monitoring import RecordingMonitoring


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
    supports_form_notifications = True

    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return []


class FormHookChannel(RichChannel):
    """A form-capable channel that ALSO declares the OPTIONAL ``validate_form_schema``
    hook, recording every ``(schema, question)`` pair the notify door hands it and
    refusing a schema naming a ``reserved`` property — a stand-in for a medium's own
    form limits the shared subset walk does not know."""

    def __init__(self) -> None:
        super().__init__()
        self.hook_calls: list[tuple[dict, str]] = []

    def validate_form_schema(self, schema: dict, question: str) -> None:
        self.hook_calls.append((schema, question))
        if "reserved" in schema.get("properties", {}):
            raise ValueError("form schema property 'reserved' is a reserved name on this channel")


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
_TEMPLATE = ChannelTemplate(name="status_update", language="en_US", body_parameters=["A-42"])


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

    await notify_user("pick one", channel="rich", options=[ReplyOption(text="Item A"), ReplyOption(text="Item B")])

    assert channel.notifications == [
        ChannelNotification(message="pick one", options=[ReplyOption(text="Item A"), ReplyOption(text="Item B")])
    ]


async def test_notify_threads_options_with_media_to_a_capable_channel(register_channel):
    channel = register_channel("rich", RichChannel())

    await notify_user("a card with a list", channel="rich", media=[_IMAGE], options=[ReplyOption(text="Item A")])

    assert channel.notifications == [
        ChannelNotification(message="a card with a list", media=[_IMAGE], options=[ReplyOption(text="Item A")])
    ]


async def test_media_only_notify_sends_blank_message_with_media(register_channel):
    # The contract admits a blank message exactly when media carries the content (a
    # caption-less image); the door mirrors that rule rather than refusing blank outright.
    channel = register_channel("rich", RichChannel())

    await notify_user("", channel="rich", media=[_IMAGE])

    assert channel.notifications == [ChannelNotification(message="", media=[_IMAGE])]


async def test_media_only_notify_records_to_sink(sink_redis):
    # The sink path admits the same media-only shape: a blank message stored with its media.
    await notify_user("", media=[_IMAGE])

    records = await notifications_sink.read_notifications()
    assert len(records) == 1
    assert records[0]["message"] == ""
    assert records[0]["media"] == [_IMAGE.model_dump(mode="json")]


async def test_blank_message_with_options_still_refused(register_channel):
    # The contract's options-need-a-prompt rule holds at the door: a blank message may ride
    # only media, never options.
    channel = register_channel("rich", RichChannel())

    with pytest.raises(ValueError, match="carries no options"):
        await notify_user("", channel="rich", media=[_IMAGE], options=[ReplyOption(text="Item A")])
    assert channel.notifications == []


async def test_options_to_channel_without_capability_is_not_implemented(register_channel):
    # A channel that does not advertise supports_interactive_notifications refuses the
    # options send loudly (NotImplementedError → 501) — never a silent text downgrade.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support interactive notifications"):
        await notify_user("pick one", channel="plain", options=[ReplyOption(text="Item A"), ReplyOption(text="Item B")])
    assert channel.notifications == []


async def test_options_capability_guard_fires_before_the_feed_record(register_channel, sink_redis):
    # A refused options send leaves NO phantom feed entry: the guard precedes the audience
    # in-app record, so an audience-addressed options send to an incapable channel writes
    # nothing.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support interactive notifications"):
        await notify_user("pick one", channel="plain", options=[ReplyOption(text="Item A")], audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_options_and_template_mutual_exclusion_leaves_no_phantom_feed_entry(register_channel, sink_redis):
    # options + template both set is refused by the contract's exclusion validator (a
    # pydantic ValidationError → 400), BEFORE the audience feed record — even on a channel
    # that advertises both capabilities (so the capability guards pass).
    channel = register_channel("rich", RichChannel())

    with pytest.raises(ValidationError, match="mutually exclusive"):
        await notify_user(
            "x", channel="rich", options=[ReplyOption(text="Item A")], template=_TEMPLATE, audience="alice"
        )

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


async def test_media_with_channel_none_stored_on_sink(sink_redis):
    # Clean break: the internal sink STORES rich content (parity with the channel path)
    # rather than refusing it, so media with no named channel lands on the feed record for
    # the inbox to render — serialized as MediaItem dicts.
    await notify_user("photo", media=[_IMAGE])
    records = await notifications_sink.read_notifications()
    assert len(records) == 1
    assert records[0]["media"] == [_IMAGE.model_dump(mode="json")]
    assert records[0]["template"] is None
    assert records[0]["options"] is None


async def test_template_with_channel_none_stored_on_sink(sink_redis):
    await notify_user("hi", template=_TEMPLATE)
    records = await notifications_sink.read_notifications()
    assert records[0]["template"] == _TEMPLATE.model_dump(mode="json")
    assert records[0]["media"] is None


async def test_options_with_channel_none_stored_on_sink(sink_redis):
    await notify_user("pick one", options=[ReplyOption(text="Item A"), ReplyOption(text="Item B")])
    records = await notifications_sink.read_notifications()
    assert records[0]["options"] == [
        ReplyOption(text="Item A").model_dump(mode="json"),
        ReplyOption(text="Item B").model_dump(mode="json"),
    ]


async def test_media_and_template_mutually_exclusive_on_sink(sink_redis):
    # The contract's exclusivity still holds on the sink path — the notification the sink
    # validates through refuses media+template, and nothing is recorded.
    with pytest.raises(ValueError, match="mutually exclusive"):
        await notify_user("both", media=[_IMAGE], template=_TEMPLATE)
    assert await notifications_sink.read_notifications() == []


# -- schema (ask-less form): channel-only, capability-gated, subset + hook walked --


_FORM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


async def test_notify_threads_schema_to_a_capable_channel(register_channel):
    channel = register_channel("rich", RichChannel())

    await notify_user("fill this in", channel="rich", schema=_FORM_SCHEMA)

    assert channel.notifications == [ChannelNotification(message="fill this in", schema=_FORM_SCHEMA)]


async def test_schema_to_channel_without_capability_is_not_implemented(register_channel):
    # A channel that does not advertise supports_form_notifications refuses the form
    # send loudly (NotImplementedError → 501) — never a silent prompt-only downgrade.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support form notifications"):
        await notify_user("fill this in", channel="plain", schema=_FORM_SCHEMA)
    assert channel.notifications == []


async def test_schema_capability_guard_fires_before_the_feed_record(register_channel, sink_redis):
    # A refused form send leaves NO phantom feed entry: the guard precedes the audience
    # in-app record, exactly as the media/template/options guards do.
    channel = register_channel("plain", RecordingChannel())

    with pytest.raises(NotImplementedError, match="does not support form notifications"):
        await notify_user("fill this in", channel="plain", schema=_FORM_SCHEMA, audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_schema_outside_the_channel_subset_is_rejected(register_channel, sink_redis):
    # The shared channel-deliverable subset walk (the ONE definition the ask path uses)
    # refuses an unrenderable schema at the door — a ValueError naming the property (→ 400),
    # before any feed write and before the channel is touched.
    channel = register_channel("rich", RichChannel())
    nested = {"type": "object", "properties": {"address": {"type": "object"}}}

    with pytest.raises(ValueError, match="'address'"):
        await notify_user("fill this in", channel="rich", schema=nested, audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_schema_channel_hook_refusal_is_rejected(register_channel, sink_redis):
    # The channel's OPTIONAL validate_form_schema hook (the same one declared method the
    # ask path reuses) enforces medium-specific limits on top of the subset walk — its
    # ValueError refuses the send (→ 400) before any feed write or send.
    channel = register_channel("hooky", FormHookChannel())
    reserved = {"type": "object", "properties": {"reserved": {"type": "string"}}}

    with pytest.raises(ValueError, match="reserved name"):
        await notify_user("fill this in", channel="hooky", schema=reserved, audience="alice")

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_schema_channel_hook_receives_the_message_as_the_question(register_channel):
    # The notify door reuses the ask-time hook with the notification's MESSAGE as the
    # question (the form's prompt), so one declared method covers both surfaces.
    channel = register_channel("hooky", FormHookChannel())

    await notify_user("fill this in", channel="hooky", schema=_FORM_SCHEMA)

    assert channel.hook_calls == [(_FORM_SCHEMA, "fill this in")]
    assert channel.notifications == [ChannelNotification(message="fill this in", schema=_FORM_SCHEMA)]


async def test_schema_and_options_mutual_exclusion_leaves_no_phantom_feed_entry(register_channel, sink_redis):
    # One message carries ONE interactive surface: schema + options is refused by the
    # contract's exclusion validator (→ 400) BEFORE the audience feed record — even on a
    # channel advertising both capabilities.
    channel = register_channel("rich", RichChannel())

    with pytest.raises(ValidationError, match="mutually exclusive"):
        await notify_user(
            "x", channel="rich", schema=_FORM_SCHEMA, options=[ReplyOption(text="Item A")], audience="alice"
        )

    assert channel.notifications == []
    assert await notifications_sink.read_notifications(audience="alice") == []
    assert await notifications_sink.read_notifications() == []


async def test_schema_with_channel_none_is_refused(sink_redis):
    # A sink notification has no delivery vehicle and no submission door, so a form
    # notification REQUIRES a channel: the sink path refuses schema loudly (→ 400) and
    # records nothing — never a feed entry carrying a form nobody can submit.
    with pytest.raises(ValueError, match="needs a named channel"):
        await notify_user("fill this in", schema=_FORM_SCHEMA)
    assert await notifications_sink.read_notifications() == []


async def test_channel_send_with_audience_records_schema_on_feed(register_channel, sink_redis):
    # Feed parity: the audience-addressed channel send stores the SAME schema the channel
    # receives, so the in-app record shows the form the channel delivered.
    channel = register_channel("rich", RichChannel())

    await notify_user("fill this in", channel="rich", schema=_FORM_SCHEMA, audience="alice")

    assert channel.notifications == [ChannelNotification(message="fill this in", schema=_FORM_SCHEMA)]
    own = await notifications_sink.read_notifications(audience="alice")
    assert len(own) == 1
    assert own[0]["schema"] == _FORM_SCHEMA
    assert own[0]["template"] is None


# -- data: image substitution: stored by reference, absolute url minted here -----


_DATA_PNG = "data:image/png;base64," + base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()


async def test_notify_data_image_substituted_to_absolute_url(register_channel, monkeypatch, fake_redis):
    # A data:image on a channel send is stored by reference BEFORE the send: the channel
    # receives an ABSOLUTE served url (a vendor fetches it from its own servers), never
    # the inline bytes. The substitution lives in this shared seam, so every caller of it
    # is covered.
    channel = register_channel("rich", RichChannel())
    monkeypatch.setattr(
        notify_module, "interactions_settings", lambda: InteractionsSettings(public_base_url="https://box.example")
    )

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notify_module, "client_ctx", _ctx)
    monkeypatch.setattr(media_module.secrets, "token_urlsafe", lambda n: "N" * 43)

    await notify_user("hi", channel="rich", media=[MediaItem(kind=MediaKind.IMAGE, url=_DATA_PNG)])

    forwarded = channel.notifications[0].media
    assert forwarded is not None
    assert forwarded[0].url == "https://box.example" + MEDIA_ROUTE_PREFIX + "N" * 43


async def test_notify_data_image_http_localhost_base_validates_end_to_end(register_channel, monkeypatch, fake_redis):
    # With an http://127.0.0.1 public base (the local-dev/e2e stack), the absolute
    # served url the seam mints must validate inside ChannelNotification and reach the
    # channel — the contract admits the absolute http(s) served-reference form.
    channel = register_channel("rich", RichChannel())
    monkeypatch.setattr(
        notify_module,
        "interactions_settings",
        lambda: InteractionsSettings(public_base_url="http://127.0.0.1:8000"),
    )

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notify_module, "client_ctx", _ctx)
    monkeypatch.setattr(media_module.secrets, "token_urlsafe", lambda n: "N" * 43)

    await notify_user("hi", channel="rich", media=[MediaItem(kind=MediaKind.IMAGE, url=_DATA_PNG)])

    forwarded = channel.notifications[0].media
    assert forwarded is not None
    assert forwarded[0].url == "http://127.0.0.1:8000" + MEDIA_ROUTE_PREFIX + "N" * 43


async def test_notify_data_image_without_public_base_url_raises(register_channel, monkeypatch):
    # A data:image needs an absolute url to reach a channel; with no public base url the
    # seam refuses LOUDLY (ChannelInputError, the op maps it to a 400) naming the setting,
    # and never sends.
    channel = register_channel("rich", RichChannel())
    monkeypatch.setattr(notify_module, "interactions_settings", lambda: InteractionsSettings(public_base_url=None))

    with pytest.raises(ChannelInputError, match="INTERACTIONS_PUBLIC_BASE_URL"):
        await notify_user("hi", channel="rich", media=[MediaItem(kind=MediaKind.IMAGE, url=_DATA_PNG)])
    assert channel.notifications == []


async def test_notify_dict_media_coerced_and_data_image_substituted(register_channel, monkeypatch, fake_redis):
    # The operation door hands this seam ``media`` as plain dicts (``model_dump`` of the
    # validated request body), so the seam must coerce them to MediaItem before any
    # ``.kind``/``.url`` inspection. A dict data:image is stored by reference and the
    # channel receives an ABSOLUTE served url, exactly as with MediaItem input.
    channel = register_channel("rich", RichChannel())
    monkeypatch.setattr(
        notify_module, "interactions_settings", lambda: InteractionsSettings(public_base_url="https://box.example")
    )

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(notify_module, "client_ctx", _ctx)
    monkeypatch.setattr(media_module.secrets, "token_urlsafe", lambda n: "N" * 43)

    await notify_user(
        "hi",
        channel="rich",
        # The operation door hands this seam ``model_dump`` dicts, not MediaItem objects.
        media=cast(
            "list[MediaItem]",
            [
                {"kind": "link", "url": "https://example.com/doc", "caption": "a link"},
                {"kind": "image", "url": _DATA_PNG},
            ],
        ),
    )

    forwarded = channel.notifications[0].media
    assert forwarded is not None
    assert forwarded[0] == MediaItem(kind=MediaKind.LINK, url="https://example.com/doc", caption="a link")
    assert forwarded[1].url == "https://box.example" + MEDIA_ROUTE_PREFIX + "N" * 43


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
    with pytest.raises(ValueError, match="must be a non-empty address"):
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

    with pytest.raises(ValueError, match="address must be at most"):
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


async def test_channel_send_with_audience_records_rich_fields_on_feed(register_channel, sink_redis):
    # The channel-path feed record carries the SAME rich fields the channel receives —
    # media/options land on the in-app record, not just the channel push.
    channel = register_channel("rich", RichChannel())

    await notify_user(
        "pick one", channel="rich", media=[_IMAGE], options=[ReplyOption(text="Item A")], audience="alice"
    )

    assert channel.notifications == [
        ChannelNotification(message="pick one", media=[_IMAGE], options=[ReplyOption(text="Item A")])
    ]
    own = await notifications_sink.read_notifications(audience="alice")
    assert len(own) == 1
    assert own[0]["media"] == [_IMAGE.model_dump(mode="json")]
    assert own[0]["options"] == [ReplyOption(text="Item A").model_dump(mode="json")]
    assert own[0]["template"] is None


async def test_channel_send_with_audience_records_template_on_feed(register_channel, sink_redis):
    channel = register_channel("rich", RichChannel())

    await notify_user("your item is done", channel="rich", template=_TEMPLATE, audience="alice")

    assert channel.notifications == [ChannelNotification(message="your item is done", template=_TEMPLATE)]
    own = await notifications_sink.read_notifications(audience="alice")
    assert len(own) == 1
    assert own[0]["template"] == _TEMPLATE.model_dump(mode="json")
    assert own[0]["media"] is None


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
async def test_blank_message_without_media_rejected(register_channel, bad_message):
    # The contract's blank-vs-media rule: with no media to carry the content, a blank
    # message has nothing to deliver and the door refuses it before any send.
    channel = register_channel("fake", RecordingChannel())

    with pytest.raises(ValueError, match="message must be non-blank unless media or location"):
        await notify_user(bad_message, channel="fake")
    assert channel.notifications == []


async def test_non_string_message_rejected(register_channel):
    channel = register_channel("fake", RecordingChannel())

    with pytest.raises(ValueError, match="message must be a string"):
        await notify_user(42, channel="fake")  # type: ignore[arg-type]
    assert channel.notifications == []


# -- send-outcome monitoring (tier 1 span + tier 2 receipt index) -----------------


@pytest.fixture
def recording_backend(monkeypatch):
    """Install a recording monitoring backend and stub the tier-2 index write so the
    seam's call args can be asserted without a Redis. Reset around the test."""
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    indexed: list[dict] = []

    async def _index(channel, provider_message_ids, *, trace_id, span_id):
        indexed.append({"channel": channel, "ids": provider_message_ids, "trace_id": trace_id, "span_id": span_id})

    monkeypatch.setattr(notify_module, "index_flow_send", _index)
    backend.indexed = indexed  # type: ignore[attr-defined]
    yield backend
    reset_monitoring()


async def test_notify_emits_send_span_and_indexes_ids(register_channel, recording_backend):
    recording_backend.writer.active_trace_id = "trace-1"
    recording_backend.writer.next_span_id = "span-7"
    register_channel("ids", IdReturningChannel(["m1", "m2"]))

    assert await notify_user("split", channel="ids", recipient="+15550001111") == ["m1", "m2"]

    (recorded,) = recording_backend.writer.spans
    assert recorded["name"] == "send:ids"
    assert recorded["input"] == {"recipient": "+15550001111"}
    assert recorded["metadata"] == {"messaging.system": "ids", "messaging.operation": "send"}
    assert recorded["span"].updates[0]["output"] == {"messaging.message.id": ["m1", "m2"]}
    # Tier 2: each accepted id is indexed to this trace/span for a later receipt.
    assert recording_backend.indexed == [
        {"channel": "ids", "ids": ["m1", "m2"], "trace_id": "trace-1", "span_id": "span-7"}
    ]


async def test_notify_index_failure_does_not_fail_the_send_or_taint_the_span(
    register_channel, recording_backend, monkeypatch, caplog
):
    # The tier-2 index write is best-effort telemetry AFTER the provider ACCEPTED the send:
    # an interactions-Redis outage there must never raise a FALSE send failure (which could
    # trigger a double-send) nor mark the span ERROR. The send still returns the provider
    # ids and the span stays SUCCESS-shaped (only its success output update, no ERROR level).
    recording_backend.writer.active_trace_id = "trace-1"
    recording_backend.writer.next_span_id = "span-7"
    register_channel("ids", IdReturningChannel(["m1", "m2"]))

    async def _boom(channel, provider_message_ids, *, trace_id, span_id):
        raise ConnectionError("interactions redis down")

    monkeypatch.setattr(notify_module, "index_flow_send", _boom)

    with caplog.at_level("WARNING"):
        assert await notify_user("split", channel="ids") == ["m1", "m2"]

    # The span carries ONLY its success output update — no ERROR level from the failed index.
    (recorded,) = recording_backend.writer.spans
    assert recorded["span"].updates == [
        {"output": {"messaging.message.id": ["m1", "m2"]}, "metadata": None, "level": None, "status_message": None}
    ]
    # The failure is logged, not raised.
    assert any("flow-send receipt indexing failed" in record.message for record in caplog.records)


async def test_notify_failure_marks_error_span_and_reraises(register_channel, recording_backend):
    recording_backend.writer.active_trace_id = "trace-1"
    register_channel("boom", FailingChannel())

    with pytest.raises(ChannelDeliveryError):
        await notify_user("nope", channel="boom")

    (update,) = recording_backend.writer.spans[0]["span"].updates
    assert update["level"].value == "ERROR"
    assert update["metadata"]["error.kind"] == "delivery_failed"
    # A failed send indexes nothing (no accepted ids).
    assert recording_backend.indexed == []


async def test_notify_no_trace_emits_no_span_and_no_index(register_channel, recording_backend):
    recording_backend.writer.active_trace_id = None
    register_channel("ids", IdReturningChannel(["m1"]))

    assert await notify_user("hi", channel="ids") == ["m1"]

    assert recording_backend.writer.spans == []
    assert recording_backend.indexed == []
