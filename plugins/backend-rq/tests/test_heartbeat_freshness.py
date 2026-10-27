"""A registered worker is live only while its ``last_heartbeat`` is inside the freshness window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tai42_backend_rq.liveness import HEARTBEAT_FRESH_SECONDS, heartbeat_fresh

_WINDOW = timedelta(seconds=HEARTBEAT_FRESH_SECONDS)


def test_a_heartbeat_inside_the_window_is_fresh() -> None:
    assert heartbeat_fresh(datetime.now(UTC) - _WINDOW + timedelta(seconds=10))


def test_a_heartbeat_older_than_the_window_is_stale() -> None:
    assert not heartbeat_fresh(datetime.now(UTC) - _WINDOW - timedelta(seconds=1))


def test_a_worker_with_no_recorded_heartbeat_is_never_live() -> None:
    assert not heartbeat_fresh(None)


def test_a_naive_heartbeat_is_read_as_utc() -> None:
    """rq stores heartbeats in UTC, so a naive stamp is compared as UTC wall time."""
    now_utc = datetime.now(UTC).replace(tzinfo=None)

    assert heartbeat_fresh(now_utc - _WINDOW + timedelta(seconds=10))
    assert not heartbeat_fresh(now_utc - _WINDOW - timedelta(seconds=1))
