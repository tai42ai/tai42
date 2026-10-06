"""The ``app.conversations`` facet as a channel adapter sees it.

A medium adapter reaches the bridge only through ``tai42_app.conversations``, which must
satisfy the contract's :class:`AppConversations` Protocol and forward calls through to
the app's core with the arguments intact.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.app.facets import AppConversations
from tai42_contract.channels import ChannelDelivery, ChannelNotification
from tai42_contract.conversations import DeliveryReceipt, InboundRejectionReason

from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.app.instance import app
from tai42_skeleton.channels.inbound import _REJECTION_NOTICES, INBOUND_REJECTED_EVENT_TOPIC
from tai42_skeleton.hooks import cache as hooks_cache


class _FakeApp:
    """Stands in for ``TaiMCP`` — records what the facet forwards to the core."""

    def __init__(self) -> None:
        self.accepted: list[tuple] = []
        self.receipts: list[tuple] = []
        self.rejections: list[SimpleNamespace] = []

    async def _conversation_accept(
        self,
        channel,
        our_identity,
        client_address,
        cap_key,
        text,
        provider_message_id,
        params=None,
        form=None,
        attachments=None,
        location=None,
        locale=None,
        form_tag=None,
    ) -> str:
        self.accepted.append(
            (channel, our_identity, client_address, cap_key, text, provider_message_id, params, form, form_tag)
        )
        return "mid-1"

    async def _conversation_record_delivery_status(self, channel, provider_message_id, status) -> None:
        self.receipts.append((channel, provider_message_id, status))

    async def _conversation_notify_inbound_rejected(
        self, *, channel_id, recipient, sender_identity, kind, reason
    ) -> None:
        self.rejections.append(
            SimpleNamespace(
                channel_id=channel_id,
                recipient=recipient,
                sender_identity=sender_identity,
                kind=kind,
                reason=reason,
            )
        )


def test_the_facet_satisfies_the_contract_protocol():
    # A channel adapter type-checks against the contract Protocol alone; the runtime
    # conformance check stands in for that here.
    facet = ConversationsFacet(_FakeApp())  # type: ignore[arg-type]
    assert isinstance(facet, AppConversations)


async def test_channel_side_accept_forwards_to_the_core():
    app = _FakeApp()
    facet: AppConversations = ConversationsFacet(app)  # type: ignore[arg-type]
    message_id = await facet.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    assert message_id == "mid-1"
    # params, form and form_tag default to None and forward through the facet hop.
    assert app.accepted == [("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", None, None, None)]


async def test_channel_side_accept_forwards_params_through_the_facet_hop():
    app = _FakeApp()
    facet: AppConversations = ConversationsFacet(app)  # type: ignore[arg-type]
    params = {"token": "abc-123"}
    await facet.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", params=params)
    assert app.accepted == [
        ("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1", params, None, None)
    ]


async def test_channel_side_accept_forwards_form_through_the_facet_hop():
    app = _FakeApp()
    facet: AppConversations = ConversationsFacet(app)  # type: ignore[arg-type]
    form = {"name": "Alice"}
    await facet.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice", "PID1", form=form)
    assert app.accepted == [
        ("twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice", "PID1", None, form, None)
    ]


async def test_channel_side_accept_forwards_form_tag_through_the_facet_hop():
    app = _FakeApp()
    facet: AppConversations = ConversationsFacet(app)  # type: ignore[arg-type]
    form = {"name": "Alice"}
    await facet.accept(
        "twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice", "PID1", form=form, form_tag="ref-42"
    )
    assert app.accepted == [
        ("twilio", "+15550001111", "+15550002222", "+15550002222", "name: Alice", "PID1", None, form, "ref-42")
    ]


async def test_channel_side_record_delivery_status_forwards_to_the_core():
    app = _FakeApp()
    facet: AppConversations = ConversationsFacet(app)  # type: ignore[arg-type]
    await facet.record_delivery_status("twilio", "out-9", DeliveryReceipt.FAILED)
    assert app.receipts == [("twilio", "out-9", DeliveryReceipt.FAILED)]


async def test_channel_side_notify_inbound_rejected_forwards_to_the_core():
    app_stub = _FakeApp()
    facet: AppConversations = ConversationsFacet(app_stub)  # type: ignore[arg-type]
    await facet.notify_inbound_rejected(
        channel_id="twilio",
        recipient="+15550002222",
        sender_identity="+15550001111",
        kind="image",
        reason=InboundRejectionReason.UNSUPPORTED_TYPE,
    )
    (rejection,) = app_stub.rejections
    assert rejection.channel_id == "twilio"
    assert rejection.recipient == "+15550002222"
    assert rejection.sender_identity == "+15550001111"
    assert rejection.kind == "image"
    assert rejection.reason is InboundRejectionReason.UNSUPPORTED_TYPE


def test_a_non_conforming_object_is_not_the_protocol():
    # Non-vacuous: the Protocol actually discriminates — a bare object is not it.
    assert not isinstance(object(), AppConversations)


@pytest.mark.parametrize("method", ["accept", "record_delivery_status", "notify_inbound_rejected"])
def test_the_facet_exposes_exactly_the_channel_surface(method):
    facet = ConversationsFacet(_FakeApp())  # type: ignore[arg-type]
    assert callable(getattr(facet, method))


class _RecordingChannel:
    """A registered channel that records every participant notice handed to ``notify``."""

    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def deliver(self, delivery: ChannelDelivery) -> None:  # pragma: no cover - unused
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return []


class _FakeHooksManager:
    """Captures every ``on_event`` the chokepoint emits."""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


@pytest.fixture
def rejection_wired(monkeypatch):
    """Bind the real app, register a recording participant channel and capture the platform-event
    seam, so a call through ``tai42_app.conversations`` runs the whole facet -> forwarder ->
    chokepoint path for real."""
    app._channel_registry.reset()
    channel = _RecordingChannel()
    hooks = _FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    with tai42_app.bound(app):
        tai42_app.channels.register("fakechan", channel)
        yield SimpleNamespace(channel=channel, events=hooks.events)
    app._channel_registry.reset()


@pytest.mark.parametrize(
    ("reason", "copy"),
    [
        (InboundRejectionReason.UNSUPPORTED_TYPE, "This content type is not supported here."),
        (InboundRejectionReason.TOO_LARGE, "That file is too large to accept here."),
        (InboundRejectionReason.COULD_NOT_RECEIVE, "That attachment could not be received."),
    ],
)
async def test_notify_inbound_rejected_replies_with_the_reason_copy(rejection_wired, reason, copy):
    await tai42_app.conversations.notify_inbound_rejected(
        channel_id="fakechan",
        recipient="+15550001111",
        sender_identity="op-1",
        kind="image",
        reason=reason,
    )

    # (a) the participant gets the ONE fixed notice for the reason, from the ask's identity.
    assert rejection_wired.channel.notifications == [
        ChannelNotification(message=copy, recipient="+15550001111", sender_identity="op-1")
    ]
    assert copy == _REJECTION_NOTICES[reason]

    # (b) the platform event fires EXACTLY ONCE on the distinct topic, carrying the generic
    # kind + reason (never the participant reply text).
    (event,) = rejection_wired.events
    assert event.topic == INBOUND_REJECTED_EVENT_TOPIC == "conversations_inbound_rejected"
    assert event.payload == {
        "channel": "fakechan",
        "client_address": "+15550001111",
        "kind": "image",
        "reason": reason.value,
    }


async def test_notify_inbound_rejected_raises_on_an_unknown_channel(rejection_wired):
    # A missing channel raises the same way a missing route does on the accept path — never a
    # silently dropped notice.
    with pytest.raises(KeyError):
        await tai42_app.conversations.notify_inbound_rejected(
            channel_id="not-registered",
            recipient="+15550001111",
            sender_identity="op-1",
            kind="image",
            reason=InboundRejectionReason.UNSUPPORTED_TYPE,
        )
    assert rejection_wired.events == []
