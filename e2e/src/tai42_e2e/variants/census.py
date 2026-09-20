"""The app worker-bus census and broker lease."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import redis

from tai42_e2e.rabbitx import RabbitAdmin


@dataclass(frozen=True)
class BusWorker:
    """One live worker on the app-owned worker bus, parsed from a presence key + value:
    the slot ``name`` (``{kind}-{n}``, the lowest-free ordinal held for one claim's life)
    off the key, and the presence value's ``kind`` (``serve`` for an HTTP worker,
    ``backend`` for a runtime worker), ``pid``, ``generation`` (the monotonic life counter
    minted with the claim), lifecycle ``state`` (``ready`` / ``resyncing`` / ``recycling``),
    the ``joined_at`` / ``beat_at`` timestamps, and the optional ``last_op`` summary."""

    name: str
    kind: str
    pid: int
    generation: int
    joined_at: str
    beat_at: str
    state: str
    last_op: dict[str, Any] | None = None


def bus_census(bus_redis_url: str, namespace: str) -> list[BusWorker]:
    """The live fleet on the worker bus: scan the per-name presence keys under this
    stack's namespace on the bus Redis and parse each value's
    ``{kind, pid, generation, joined_at, beat_at, state, last_op?}``.

    Backend-independent — every subscribed worker (both the HTTP ``serve`` workers and
    the ``backend`` runtime) advertises exactly one presence key under its slot name, so
    this is the whole fleet the reload/readiness seams draw from, read straight off the
    bus Redis (the harness never imports the system under test). A key that expires
    between the scan and the value read is skipped; a malformed value raises loudly
    (a stack scans only its own namespace, so it sees only its own well-formed keys)."""
    prefix = f"{namespace}:bus:presence:"
    client = redis.Redis.from_url(bus_redis_url, decode_responses=True)
    try:
        workers: list[BusWorker] = []
        for key in client.scan_iter(match=f"{prefix}*"):
            value = client.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"presence value for {key!r} is not a decoded string: {type(value)!r}")
            meta = json.loads(value)
            workers.append(
                BusWorker(
                    name=key[len(prefix) :],
                    kind=meta["kind"],
                    pid=int(meta["pid"]),
                    generation=int(meta["generation"]),
                    joined_at=meta["joined_at"],
                    beat_at=meta["beat_at"],
                    state=meta["state"],
                    last_op=meta.get("last_op"),
                )
            )
        return workers
    finally:
        client.close()


def short_presence_ttl_env(seconds: float) -> dict[str, str]:
    """Env that makes a frozen or killed worker leave the bus census within
    ~``seconds``: a subscriber refreshes its presence key at a third of
    ``TAI_BUS_HEARTBEAT_TTL``, so a stopped worker's key expires within one TTL.

    Backend-independent — presence + its TTL live on the app-owned bus, not in any
    plugin, so the same env governs every backend and every worker kind."""
    return {"TAI_BUS_HEARTBEAT_TTL": str(seconds)}


@dataclass(frozen=True)
class BrokerLease:
    """A per-stack broker reservation. ``release()`` reaps the isolated broker
    resource in ``TaiStack.teardown``'s error-collecting block, symmetric to the
    per-stack Postgres database drop."""

    broker_url: str
    admin: RabbitAdmin
    vhost: str

    def release(self) -> None:
        self.admin.delete_vhost(self.vhost)
