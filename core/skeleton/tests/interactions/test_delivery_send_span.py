"""The ask_user channel-delivery send seam (tier 1 of the send-outcome layer): one
``send:<channel>`` span PER delivery ATTEMPT, so a retried delivery shows each attempt's
outcome (attempt 1 failed retryable, attempt 2 accepted) rather than one collapsed span.

Driven through an ASYNC park, which runs the full delivery retry loop and then returns a
``SuspendedInteraction`` immediately — exercising the seam without the blocking answer wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification
from tai42_contract.interactions import (
    SuspendedInteraction,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)

from tai42_skeleton.app.instance import app
from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import ask_user
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring

from .._fakes.recording_monitoring import RecordingMonitoring


class _FlakyChannel:
    """Fails the first delivery attempt with a retryable error, accepts the second."""

    def __init__(self) -> None:
        self.attempts = 0

    async def deliver(self, delivery: ChannelDelivery) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise ChannelDeliveryError("transient 503", retryable=True)

    async def notify(self, notification: ChannelNotification) -> None:  # pragma: no cover - unused here
        return None


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def driver():
    tool_token = set_resume_continuation_tool("resume_tool")
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    yield
    reset_execution_identity(id_token)
    reset_resume_continuation_tool(tool_token)


@pytest.fixture
def backend():
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


@pytest.fixture
def register_channel():
    # Bind the app for the test's duration: registration goes through the ``tai42_app`` handle,
    # so this module cannot lean on some EARLIER module's import-time bind — that only holds
    # when the whole member suite runs, and leaves this test failing on its own.
    app._channel_registry.reset()

    def _register(name, channel):
        tai42_app.channels.register(name, channel)
        return channel

    with tai42_app.bound(app):
        yield _register
    app._channel_registry.reset()


async def test_one_span_per_delivery_attempt(monkeypatch, fake_client_ctx, driver, backend, register_channel):
    backend.writer.active_trace_id = "trace-1"
    settings = InteractionsSettings(
        public_base_url="https://host.example",
        delivery_max_attempts=3,
        delivery_retry_backoff_seconds=0.01,
    )
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    channel = register_channel("flaky", _FlakyChannel())

    result = await ask_user(
        "proceed?",
        channel="flaky",
        recipient="+15550001111",
        mode="async",
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert isinstance(result, SuspendedInteraction)
    assert channel.attempts == 2

    # One span per attempt: attempt 1 ERROR (retryable), attempt 2 accepted (no ERROR).
    assert [s["name"] for s in backend.writer.spans] == ["send:flaky", "send:flaky"]
    assert [s["metadata"]["retry.attempt"] for s in backend.writer.spans] == [1, 2]
    first_updates = backend.writer.spans[0]["span"].updates
    assert first_updates[0]["level"].value == "ERROR"
    assert first_updates[0]["metadata"]["retryable"] is True
    # The accepted attempt's span carries no ERROR update.
    assert all(u["level"] is None for u in backend.writer.spans[1]["span"].updates)
    # The recipient rides the input path on every attempt.
    assert backend.writer.spans[0]["input"] == {"recipient": "+15550001111"}
