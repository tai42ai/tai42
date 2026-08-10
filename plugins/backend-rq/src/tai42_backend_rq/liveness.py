"""The heartbeat-freshness contract that decides whether a registered RQ worker
is actually alive.

RQ registers a worker's death only on a graceful exit, so a SIGKILLed worker's
``rq:worker:<name>`` registry entry survives until its key expiry (minutes) and
``Worker.all()`` keeps returning it. A live worker refreshes ``last_heartbeat``
on every idle dequeue poll and every ``job_monitoring_interval`` while running a
job, so a heartbeat older than the window below proves a registered worker is a
registry ghost, not a live process. The ``backend_*`` worker tools report
liveness with this one window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rq.defaults import DEFAULT_JOB_MONITORING_INTERVAL

# The longest gap a healthy worker leaves between heartbeats is one
# ``job_monitoring_interval`` (its in-job refresh tick), plus RQ's 60s margin —
# the same ``interval + 60`` formula RQ sizes the heartbeat key's TTL with, so a
# fresh heartbeat implies the registry key still exists.
HEARTBEAT_FRESH_SECONDS = DEFAULT_JOB_MONITORING_INTERVAL + 60


def heartbeat_fresh(last_heartbeat: datetime | None) -> bool:
    """Whether a worker's ``last_heartbeat`` is recent enough to call it live. No
    recorded heartbeat is never live; a naive timestamp is read as UTC (RQ stores
    heartbeats in UTC)."""
    if last_heartbeat is None:
        return False
    if last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last_heartbeat) < timedelta(seconds=HEARTBEAT_FRESH_SECONDS)
