"""The best-effort answer-rejected guest notice seam (``_notify_guest``), wrapped in the
tier-1 send span.

In the inbound-webhook context there is no ambient flow trace, so the span is a gated
no-op — the notice is covered honestly without fabricating a rootless span, and a notify
failure is still swallowed (best-effort). Under an active trace the failure is recorded.
"""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelNotification

from tai42_skeleton.app.instance import app
from tai42_skeleton.channels.inbound import InboundBridge, _notify_guest
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring

from .._fakes.recording_monitoring import RecordingMonitoring


class _RecordingChannel:
    def __init__(self) -> None:
        self.notifications: list[ChannelNotification] = []

    async def notify(self, notification: ChannelNotification) -> None:
        self.notifications.append(notification)


class _FailingChannel:
    async def notify(self, notification: ChannelNotification) -> None:
        raise RuntimeError("provider unreachable")


def _bridge() -> InboundBridge:
    return InboundBridge(
        channel_id="fakechan",
        our_identity="op-1",
        client_address="+15550001111",
        cap_key="+15550001111",
        provider_message_id="prov-msg-1",
        bridge_text="hello there",
    )


@pytest.fixture
def backend():
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


@pytest.fixture
def register_channel():
    app._channel_registry.reset()

    def _register(channel):
        tai42_app.channels.register("fakechan", channel)
        return channel

    yield _register
    app._channel_registry.reset()


async def test_notice_sends_and_span_no_ops_without_a_trace(backend, register_channel):
    backend.writer.active_trace_id = None  # webhook context: no ambient trace
    channel = register_channel(_RecordingChannel())

    await _notify_guest(_bridge(), "your answer could not be accepted")

    assert channel.notifications == [
        ChannelNotification(
            message="your answer could not be accepted", recipient="+15550001111", sender_identity="op-1"
        )
    ]
    assert backend.writer.spans == []  # gated no-op


async def test_notify_failure_is_swallowed_and_recorded_under_a_trace(backend, register_channel):
    backend.writer.active_trace_id = "trace-1"
    register_channel(_FailingChannel())

    # Best-effort: the failure never propagates out of the notice.
    await _notify_guest(_bridge(), "closed")

    (update,) = backend.writer.spans[0]["span"].updates
    assert update["level"].value == "ERROR"
