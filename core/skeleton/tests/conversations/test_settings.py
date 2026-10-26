"""The ``CONVERSATIONS_*`` settings: the configurable bounds and their defaults, and the
backend-selection property."""

from __future__ import annotations

from tai42_skeleton.conversations.settings import ConversationsSettings


def test_default_bounds_match_the_design():
    s = ConversationsSettings()
    assert s.max_concurrent_turns == 10
    assert s.thread_queue_depth == 20
    assert s.per_address_turns_per_hour == 20
    assert s.sync_wait_max_seconds == 120
    assert s.delivery_max_attempts == 8
    assert s.delivery_grace_seconds == 3600
    assert s.inbound_dedupe_ttl_seconds >= 48 * 3600
    assert s.answer_retention_ttl_seconds == 30 * 86400
    # No hardcoded per-channel table: the override map is empty by default; each channel declares
    # its own cap through the ``max_message_chars`` seam (an operator entry here overrides it).
    assert s.max_message_chars == {}


def test_the_delivery_backoff_schedule_spans_an_hour():
    from tai42_skeleton.conversations.delivery import _backoff_seconds

    # A receiver down for an hour is still retried before its answer fails. The waits
    # sit BETWEEN attempts, so there are ``delivery_max_attempts - 1`` of them.
    s = ConversationsSettings()
    waits = [_backoff_seconds(s, attempt) for attempt in range(1, s.delivery_max_attempts)]
    assert waits == [60, 120, 240, 480, 900, 900, 900]
    assert sum(waits) == 3600


def test_in_memory_is_true_without_a_redis_url():
    # No CONVERSATIONS_REDIS_URL configured -> no durable backend.
    assert ConversationsSettings().in_memory is True


def test_bounds_reject_non_positive(monkeypatch):
    import pytest
    from pydantic import ValidationError

    monkeypatch.setenv("CONVERSATIONS_THREAD_QUEUE_DEPTH", "0")
    with pytest.raises(ValidationError):
        ConversationsSettings()


def test_the_overlap_cancel_poll_default_and_key():
    s = ConversationsSettings()
    assert s.overlap_cancel_poll_seconds == 0.5
    # The marker sits under its own segment; the ``:``-bearing thread id sits LAST.
    assert s.keyspace.overlap_cancel_key("bridge:line:+1555") == "conversations:overlap:cancel:bridge:line:+1555"


def test_an_overlap_cancel_poll_at_or_above_the_thread_lease_is_refused(monkeypatch):
    import pytest
    from pydantic import ValidationError

    # A poll interval as long as the lease could let the cancel marker expire before the next
    # read, so a running turn that should have been cancelled runs on — refused at startup.
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_SECONDS", "10")
    monkeypatch.setenv("CONVERSATIONS_OVERLAP_CANCEL_POLL_SECONDS", "10")
    with pytest.raises(ValidationError, match="must be below"):
        ConversationsSettings()


def test_a_non_positive_split_cap_is_refused_at_startup(monkeypatch):
    import pytest
    from pydantic import ValidationError

    # A cap no send can be split against is a config error at startup, not at send time
    # where it would strand the record it fails.
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", '{"twilio": 0}')
    with pytest.raises(ValidationError, match="must be positive"):
        ConversationsSettings()
