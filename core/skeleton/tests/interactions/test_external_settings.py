"""The new external-interactions settings bind from ``INTERACTIONS_*`` env."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tai42_skeleton.interactions.settings import InteractionsSettings


def test_defaults():
    s = InteractionsSettings()
    assert s.public_base_url is None
    assert s.callback_max_body_bytes == 65536
    assert s.max_concurrent is None
    assert s.delivery_max_attempts == 3
    assert s.delivery_retry_backoff_seconds == 1.0


def test_env_binding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INTERACTIONS_PUBLIC_BASE_URL", "https://host.example")
    monkeypatch.setenv("INTERACTIONS_CALLBACK_MAX_BODY_BYTES", "1024")
    monkeypatch.setenv("INTERACTIONS_MAX_CONCURRENT", "7")
    monkeypatch.setenv("INTERACTIONS_DELIVERY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("INTERACTIONS_DELIVERY_RETRY_BACKOFF_SECONDS", "0.25")

    s = InteractionsSettings()
    assert s.public_base_url == "https://host.example"
    assert s.callback_max_body_bytes == 1024
    assert s.max_concurrent == 7
    assert s.delivery_max_attempts == 5
    assert s.delivery_retry_backoff_seconds == 0.25


def test_single_delivery_attempt_accepted():
    # 1 = one attempt, no retry — the floor, not a rejection.
    assert InteractionsSettings(delivery_max_attempts=1).delivery_max_attempts == 1


@pytest.mark.parametrize("value", [0, -1])
def test_delivery_max_attempts_below_one_rejected(value: int):
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        InteractionsSettings(delivery_max_attempts=value)


@pytest.mark.parametrize("value", [0, -1.5])
def test_non_positive_delivery_backoff_rejected(value: float):
    with pytest.raises(ValidationError, match="greater than 0"):
        InteractionsSettings(delivery_retry_backoff_seconds=value)


@pytest.mark.parametrize(
    "field",
    [
        "answer_timeout_seconds",
        "idle_ttl_seconds",
        "callback_max_body_bytes",
        "max_concurrent",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_ints_rejected(field: str, value: int):
    kwargs: dict[str, Any] = {field: value}
    with pytest.raises(ValidationError, match="greater than 0"):
        InteractionsSettings(**kwargs)


def test_public_base_url_http_non_localhost_rejected():
    with pytest.raises(ValidationError, match="must be https"):
        InteractionsSettings(public_base_url="http://evil.example")


@pytest.mark.parametrize("base", ["https://host.example", "http://localhost:8080", "http://127.0.0.1"])
def test_public_base_url_accepted(base: str):
    assert InteractionsSettings(public_base_url=base).public_base_url == base
