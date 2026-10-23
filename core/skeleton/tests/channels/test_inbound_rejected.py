"""The reason-typed inbound-rejection chokepoint ``notify_inbound_rejected``.

The ONE seam a channel calls when it recognises an inbound content but cannot bridge it
as a turn: it sends the participant the one fixed notice for the reason and emits the
``conversations_inbound_rejected`` platform event. The send and the emit are each
best-effort — a channel-``notify`` fault or a hooks-manager fault never turns a handled
rejection into a lost inbound webhook — while an unknown channel raises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification, SendOutcome
from tai42_contract.conversations import (
    ChannelConversationRoute,
    ConversationRoute,
    InboundRejectionReason,
    ReplyTexts,
)
from tests._helpers import AddressingChannel

from tai42_skeleton.app.instance import app
from tai42_skeleton.channels.inbound import INBOUND_REJECTED_EVENT_TOPIC, notify_inbound_rejected
from tai42_skeleton.conversations import cache as conversations_cache
from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.hooks import cache as hooks_cache


class _RecordingChannel(AddressingChannel):
    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:  # pragma: no cover - unused
        return self.outcome(delivery, [])

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
        self.notifications.append(notification)
        return self.outcome(notification, [])


class _FailingNotifyChannel(AddressingChannel):
    def __init__(self) -> None:
        self.calls = 0

    async def deliver(self, delivery: ChannelDelivery) -> SendOutcome:  # pragma: no cover - unused
        return self.outcome(delivery, [])

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
        self.calls += 1
        raise RuntimeError("provider unreachable")


class _FakeHooksManager:
    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.events.append(SimpleNamespace(topic=topic, payload=payload))


class _FailingHooksManager:
    def __init__(self) -> None:
        self.calls = 0

    async def on_event(self, topic, payload, *, tool_kwargs_override=None):
        self.calls += 1
        raise RuntimeError("hooks down")


class _RoutesManager:
    """A conversations manager double serving only the route table the rejection notice resolves."""

    durable = True

    def __init__(self, routes: list[ConversationRoute]) -> None:
        self.routes = {route.route_name: route for route in routes}

    async def list_routes(self) -> tuple[dict[str, ConversationRoute], int]:
        return dict(self.routes), 0


class _BrokenRoutesManager:
    durable = True

    async def list_routes(self) -> tuple[dict[str, ConversationRoute], int]:
        raise RuntimeError("route store down")


def _line(reply_texts: ReplyTexts | None = None, our_identity: str = "op-1") -> ChannelConversationRoute:
    return ChannelConversationRoute(
        route_name="line-a",
        door="channel",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc",
        channel="fakechan",
        our_identity=our_identity,
        execution_key_fingerprint="fp-1",
        reply_texts=reply_texts,
    )


@pytest.fixture(autouse=True)
def route_table(monkeypatch):
    """Serve a route table holding one plain line for ``op-1``; a test swaps in its own routes."""
    manager = _RoutesManager([_line()])
    monkeypatch.setattr(conversations_cache, "get_conversations_manager", lambda: manager)
    return manager


@pytest.fixture
def register_channel(monkeypatch):
    app._channel_registry.reset()

    def _register(channel):
        tai42_app.channels.register("fakechan", channel)
        return channel

    yield _register
    app._channel_registry.reset()


async def test_notify_failure_is_swallowed_and_the_event_still_fires(register_channel, monkeypatch):
    channel = register_channel(_FailingNotifyChannel())
    hooks = _FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    # Best-effort: the notify fault never propagates out of the chokepoint.
    await notify_inbound_rejected(
        channel_id="fakechan",
        recipient="+15550001111",
        sender_identity="op-1",
        kind="document",
        reason=InboundRejectionReason.TOO_LARGE,
    )

    assert channel.calls == 1
    # The operator event still fires despite the failed participant notice.
    (event,) = hooks.events
    assert event.topic == INBOUND_REJECTED_EVENT_TOPIC
    assert event.payload["reason"] == InboundRejectionReason.TOO_LARGE.value


async def test_emit_failure_is_swallowed_after_the_notice_is_sent(register_channel, monkeypatch):
    channel = register_channel(_RecordingChannel())
    hooks = _FailingHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    # Best-effort: the hooks-manager fault never propagates out of the chokepoint.
    await notify_inbound_rejected(
        channel_id="fakechan",
        recipient="+15550001111",
        sender_identity="op-1",
        kind="audio",
        reason=InboundRejectionReason.COULD_NOT_RECEIVE,
    )

    # The participant notice was sent before the emit failed.
    assert channel.notifications == [
        ChannelNotification(
            message="That attachment could not be received.", recipient="+15550001111", sender_identity="op-1"
        )
    ]
    assert hooks.calls == 1


async def test_unknown_channel_raises_before_any_emit(register_channel, monkeypatch):
    register_channel(_RecordingChannel())
    hooks = _FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)

    with pytest.raises(KeyError):
        await notify_inbound_rejected(
            channel_id="not-registered",
            recipient="+15550001111",
            sender_identity="op-1",
            kind="image",
            reason=InboundRejectionReason.UNSUPPORTED_TYPE,
        )
    assert hooks.events == []


_ROUTE_WORDS = ReplyTexts(
    rejected_unsupported_type="Este tipo de contenido no se admite aquí.",
    rejected_too_large="Ese archivo es demasiado grande.",
    rejected_could_not_receive="No se pudo recibir ese adjunto.",
)


@pytest.mark.parametrize(
    ("reason", "member"),
    [
        (InboundRejectionReason.UNSUPPORTED_TYPE, "rejected_unsupported_type"),
        (InboundRejectionReason.TOO_LARGE, "rejected_too_large"),
        (InboundRejectionReason.COULD_NOT_RECEIVE, "rejected_could_not_receive"),
    ],
)
async def test_each_refusal_notice_draws_the_route_words(register_channel, monkeypatch, route_table, reason, member):
    channel = register_channel(_RecordingChannel())
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: _FakeHooksManager())
    # The identity arrives padded; the route is keyed by its canonical form.
    route_table.routes = {"line-a": _line(_ROUTE_WORDS)}

    await notify_inbound_rejected(
        channel_id="fakechan",
        recipient="+15550001111",
        sender_identity="  op-1 ",
        kind="document",
        reason=reason,
    )

    (notification,) = channel.notifications
    assert notification.message == getattr(_ROUTE_WORDS, member)


async def test_a_route_without_the_member_sends_the_built_in_notice(register_channel, monkeypatch, route_table):
    channel = register_channel(_RecordingChannel())
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: _FakeHooksManager())
    route_table.routes = {"line-a": _line(ReplyTexts(rejected_too_large="Ese archivo es demasiado grande."))}

    await notify_inbound_rejected(
        channel_id="fakechan",
        recipient="+15550001111",
        sender_identity="op-1",
        kind="image",
        reason=InboundRejectionReason.UNSUPPORTED_TYPE,
    )

    (notification,) = channel.notifications
    assert notification.message == "This content type is not supported here."


async def test_an_identity_with_no_route_warns_and_sends_the_built_in_notice(
    register_channel, monkeypatch, route_table, caplog
):
    channel = register_channel(_RecordingChannel())
    hooks = _FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    route_table.routes = {"line-a": _line(_ROUTE_WORDS, our_identity="op-2")}

    with caplog.at_level("WARNING", logger="tai42_skeleton.channels.inbound"):
        await notify_inbound_rejected(
            channel_id="fakechan",
            recipient="+15550001111",
            sender_identity="op-1",
            kind="image",
            reason=InboundRejectionReason.TOO_LARGE,
        )

    (notification,) = channel.notifications
    assert notification.message == "That file is too large to accept here."
    assert any("'fakechan'" in record.getMessage() and "op-1" in record.getMessage() for record in caplog.records)
    (event,) = hooks.events
    assert event.payload["reason"] == InboundRejectionReason.TOO_LARGE.value


async def test_a_deployment_without_a_routing_store_warns_and_sends_the_built_in_notice(
    register_channel, monkeypatch, caplog
):
    channel = register_channel(_RecordingChannel())
    hooks = _FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    manager = InMemoryConversationsManager(ConversationsSettings())
    monkeypatch.setattr(conversations_cache, "get_conversations_manager", lambda: manager)

    with caplog.at_level("WARNING", logger="tai42_skeleton.channels.inbound"):
        await notify_inbound_rejected(
            channel_id="fakechan",
            recipient="+15550001111",
            sender_identity="op-1",
            kind="sticker",
            reason=InboundRejectionReason.UNSUPPORTED_TYPE,
        )

    assert channel.notifications == [
        ChannelNotification(
            message="This content type is not supported here.", recipient="+15550001111", sender_identity="op-1"
        )
    ]
    assert any(
        "no conversation routing store" in record.getMessage() and "'fakechan'" in record.getMessage()
        for record in caplog.records
    )
    (event,) = hooks.events
    assert event.payload["reason"] == InboundRejectionReason.UNSUPPORTED_TYPE.value


async def test_a_route_resolution_fault_propagates_before_any_send(register_channel, monkeypatch):
    channel = register_channel(_RecordingChannel())
    hooks = _FakeHooksManager()
    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: hooks)
    monkeypatch.setattr(conversations_cache, "get_conversations_manager", lambda: _BrokenRoutesManager())

    with pytest.raises(RuntimeError, match="route store down"):
        await notify_inbound_rejected(
            channel_id="fakechan",
            recipient="+15550001111",
            sender_identity="op-1",
            kind="image",
            reason=InboundRejectionReason.TOO_LARGE,
        )
    assert channel.notifications == []
    assert hooks.events == []
