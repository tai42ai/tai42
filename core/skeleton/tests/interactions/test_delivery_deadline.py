"""The ask dispatcher never hands a channel a delivery whose deadline has passed.

``deliver_with_retry`` is the only caller of ``Channel.deliver``; a neutral recording channel
proves the refusal for a past sync deadline, a past async expiry, and a deadline that passes during
the retry backoff.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.interactions.ask import delivery as delivery_module
from tai42_skeleton.interactions.ask.delivery import deliver_with_retry
from tai42_skeleton.interactions.ask.timing import DeadlineWindow
from tai42_skeleton.interactions.store import InteractionStore


class _RecordingChannel:
    """A neutral channel that records each delivery and fails each one with ``failures`` in turn."""

    def __init__(self, failures: list[BaseException] | None = None) -> None:
        self.delivered: list[ChannelDelivery] = []
        self._failures = list(failures or [])

    async def deliver(self, delivery: ChannelDelivery) -> None:
        self.delivered.append(delivery)
        if self._failures:
            raise self._failures.pop(0)

    async def notify(self, notification: Any) -> list[str]:  # pragma: no cover - not reached
        return []


def _frame(timeout_at: datetime) -> ChannelDelivery:
    return ChannelDelivery(
        interaction_id="i-1",
        question="?",
        answer_format="text",
        callback_url="https://h.example/api/interactions/callback/tk",
        timeout_at=timeout_at,
    )


def _window(seconds: float = 30.0) -> DeadlineWindow:
    now = datetime.now(UTC)
    loop = asyncio.get_running_loop()
    return DeadlineWindow(
        budget=seconds,
        created_at=now,
        timeout_at=now + timedelta(seconds=seconds),
        deadline=loop.time() + seconds,
        park_ttl_margin_seconds=0,
    )


@pytest.fixture
def pruned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def _prune(store: Any, interaction_id: str, group_id: str) -> str:
        calls.append(interaction_id)
        return "pruned"

    async def _emit(**kwargs: Any) -> None:
        calls.append(f"event:{kwargs['interaction_id']}")

    monkeypatch.setattr(delivery_module, "prune", _prune)
    monkeypatch.setattr(delivery_module, "emit_delivery_failed", _emit)
    return calls


async def _deliver(channel: _RecordingChannel, frame: ChannelDelivery) -> None:
    await deliver_with_retry(
        channel,
        frame,
        InteractionStore("tai:interactions"),
        _window(),
        channel="probe",
        recipient=None,
        interaction_id="i-1",
        group="g-1",
        question="?",
        sensitive=False,
    )


@pytest.mark.parametrize("past", [timedelta(seconds=1), timedelta(hours=2)])
async def test_a_delivery_past_its_deadline_is_never_sent_and_the_question_is_pruned(pruned, past):
    # A sync frame whose timeout passed, or an async park whose expiry passed: same refusal.
    channel = _RecordingChannel()
    with pytest.raises(ChannelDeliveryError, match="already timed out") as caught:
        await _deliver(channel, _frame(datetime.now(UTC) - past))
    assert channel.delivered == []
    assert caught.value.retryable is False
    assert "nothing was sent" in str(caught.value)
    assert pruned == ["i-1", "event:i-1"]


async def test_a_deadline_passing_during_the_backoff_is_refused_before_the_next_send(pruned, monkeypatch):
    # The dispatcher reads the interactions settings at each use: three attempts, 1.5 s apart.
    monkeypatch.setenv("INTERACTIONS_DELIVERY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("INTERACTIONS_DELIVERY_RETRY_BACKOFF_SECONDS", "1.5")
    reset_all_settings()
    channel = _RecordingChannel(failures=[ChannelDeliveryError("transient", retryable=True)])
    frame = _frame(datetime.now(UTC) + timedelta(seconds=1))
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await _deliver(channel, frame)
    assert len(channel.delivered) == 1
    assert pruned == ["i-1", "event:i-1"]


async def test_a_live_delivery_is_sent(pruned):
    channel = _RecordingChannel()
    await _deliver(channel, _frame(datetime.now(UTC) + timedelta(minutes=5)))
    assert len(channel.delivered) == 1
    assert pruned == []
