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
from tai42_contract.channels import ChannelDelivery, ChannelNotification
from tai42_contract.conversations import InboundRejectionReason

from tai42_skeleton.app.instance import app
from tai42_skeleton.channels.inbound import INBOUND_REJECTED_EVENT_TOPIC, notify_inbound_rejected
from tai42_skeleton.hooks import cache as hooks_cache


class _RecordingChannel:
    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def deliver(self, delivery: ChannelDelivery) -> None:  # pragma: no cover - unused
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        self.notifications.append(notification)
        return []


class _FailingNotifyChannel:
    def __init__(self) -> None:
        self.calls = 0

    async def deliver(self, delivery: ChannelDelivery) -> None:  # pragma: no cover - unused
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
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
