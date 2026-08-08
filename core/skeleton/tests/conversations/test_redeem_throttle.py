"""The redeem brute-force backoff store: failures-only, per (target, source), with a valid
redeem clearing the counter and a locked source refused without an oracle."""

from __future__ import annotations

import pytest

from tai42_skeleton.conversations import redeem_throttle as throttle_module
from tai42_skeleton.conversations.persons import PairingTarget
from tai42_skeleton.conversations.redeem_throttle import ConversationRedeemThrottle
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_TARGET = PairingTarget(target_kind="agent", target_name="concierge")
_SOURCE = '["twilio","+15550001111","+2000"]'


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")


def _throttle(monkeypatch, fake: FakeRecordRedis) -> ConversationRedeemThrottle:
    monkeypatch.setattr(throttle_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRedeemThrottle(ConversationsSettings())


async def test_failures_below_threshold_do_not_lock(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_THRESHOLD", "3")
    throttle = _throttle(monkeypatch, FakeRecordRedis())
    assert await throttle.is_locked(_TARGET, _SOURCE) is False
    for _ in range(3):
        await throttle.record_failure(_TARGET, _SOURCE)
    # Exactly at the threshold: still not locked.
    assert await throttle.is_locked(_TARGET, _SOURCE) is False


async def test_crossing_the_threshold_locks_then_clear_resets(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_THRESHOLD", "2")
    fake = FakeRecordRedis()
    throttle = _throttle(monkeypatch, fake)
    for _ in range(3):  # one past the threshold
        await throttle.record_failure(_TARGET, _SOURCE)
    assert await throttle.is_locked(_TARGET, _SOURCE) is True

    # A valid redeem clears BOTH the counter and the lock.
    await throttle.clear(_TARGET, _SOURCE)
    assert await throttle.is_locked(_TARGET, _SOURCE) is False
    assert ConversationsSettings().redeem_fail_key("agent", "concierge", _SOURCE) not in fake._strings


async def test_backoff_is_capped_and_scoped_per_source(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_THRESHOLD", "1")
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_CAP_SECONDS", "4")
    fake = FakeRecordRedis()
    throttle = _throttle(monkeypatch, fake)
    for _ in range(6):
        await throttle.record_failure(_TARGET, _SOURCE)
    lock_key = ConversationsSettings().redeem_lock_key("agent", "concierge", _SOURCE)
    assert await throttle.is_locked(_TARGET, _SOURCE) is True
    # The exponential lock never exceeds the cap.
    assert fake.ttl_ms[lock_key] <= 4000

    # A DIFFERENT source is untouched — a locked attacker never throttles an honest user.
    other = '["twilio","+15550001111","+9999"]'
    assert await throttle.is_locked(_TARGET, other) is False


async def test_backoff_escalates_exponentially_from_one_second_then_caps(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_THRESHOLD", "3")
    monkeypatch.setenv("CONVERSATIONS_REDEEM_BACKOFF_CAP_SECONDS", "16")
    fake = FakeRecordRedis()
    throttle = _throttle(monkeypatch, fake)
    lock_key = ConversationsSettings().redeem_lock_key("agent", "concierge", _SOURCE)

    # At and below the threshold nothing locks.
    for _ in range(3):
        await throttle.record_failure(_TARGET, _SOURCE)
    assert lock_key not in fake.ttl_ms
    assert await throttle.is_locked(_TARGET, _SOURCE) is False

    # The FIRST lock lands at threshold+1 ≈ 1s and each further failure DOUBLES it, capped at
    # the backoff cap (16s here) — not merely bounded below it.
    for want_ms in (1000, 2000, 4000, 8000, 16000, 16000):
        await throttle.record_failure(_TARGET, _SOURCE)
        assert await throttle.is_locked(_TARGET, _SOURCE) is True
        assert fake.ttl_ms[lock_key] == want_ms


def test_throttle_refuses_without_the_redis_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    from tai42_skeleton.operations.errors import NotSupportedError

    with pytest.raises(NotSupportedError):
        ConversationRedeemThrottle(ConversationsSettings())


def test_redeem_keyspace_is_source_scoped_and_opaque():
    settings = ConversationsSettings()
    fail_a = settings.redeem_fail_key("agent", "concierge", _SOURCE)
    lock_a = settings.redeem_lock_key("agent", "concierge", _SOURCE)
    # The fail and lock keys are distinct namespaces over the same opaque scope.
    assert fail_a != lock_a
    assert fail_a.rsplit(":", 1)[1] == lock_a.rsplit(":", 1)[1]
    # A different source, target, or kind hashes to a different scope.
    assert settings.redeem_fail_key("agent", "concierge", "other") != fail_a
    assert settings.redeem_fail_key("agent", "other", _SOURCE) != fail_a
    assert settings.redeem_fail_key("tool", "concierge", _SOURCE) != fail_a
