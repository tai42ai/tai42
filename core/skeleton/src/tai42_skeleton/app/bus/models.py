"""The worker-bus wire/value types and their pure helpers."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from tai42_contract.errors import ClientDisconnectedError

logger = logging.getLogger(__name__)

# The reserved envelope key the op payload rides under on a control-channel frame. The
# transport fields (name/generation/op_id/reply_to/targets) and the op payload occupy
# disjoint namespaces, so an op field can never collide with a route field — an op that
# carries its own ``name`` (a preset/tool name) is never clobbered by the transport
# identity ``name``.
_OP_PAYLOAD_KEY = "payload"

# Transport errors that a reconnect loop recovers from and a publish reports as the
# bus-unreachable shape. ``ClientDisconnectedError`` is the pooled ``client_ctx``
# wrapper for a severed connection, so it belongs here alongside the raw redis
# transport errors: a real bus outage arrives wrapped, and must fold into the
# bus-unreachable report (publish) and drive a reconnect + re-register (subscription),
# never escape as a bare error.
_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    RedisConnectionError,
    RedisTimeoutError,
    ClientDisconnectedError,
)


def _utcnow_iso() -> str:
    """The current instant as an ISO-8601 UTC string (presence timestamps)."""
    return datetime.now(UTC).isoformat()


def _beat_age_seconds(beat_at: str) -> float | None:
    """Seconds since a presence row's last worker-stamped beat, for a gap row's
    cosmetic detail only — never a freshness gate (that reads the raw PTTL). ``None``
    when the stamp does not parse."""
    try:
        then = datetime.fromisoformat(beat_at)
        return (datetime.now(UTC) - then).total_seconds()
    except (ValueError, TypeError):
        return None


class SlotLostError(Exception):
    """This process's slot claim was lost — a compare-token renew missed (token
    mismatch or absent claim key), so the slot already belongs to a new holder.

    Raised from the heartbeat and routed through a DEDICATED ``subscribe`` reconnect
    branch (never the transport-error tuple: a lost slot on a healthy connection is
    not an outage) so the held identity is abandoned and a NEW life is re-minted via
    subscription re-entry. A lost slot is never resumed."""


class OpOutcome(StrEnum):
    """Per-worker outcome of a fleet op.

    ``applied`` / ``failed`` are terminal WIRE replies from a subscriber.
    ``missing`` / ``departed`` / ``timed_out`` are publisher-COMPUTED from the
    presence census (a silent worker never sends them). ``resyncing`` /
    ``recycling`` / ``stale`` are the gap outcomes for a row that fails the ready+
    fresh gate: it is not an expected worker, so instead of being dropped it is
    landed as its own actual condition — ``resyncing`` (the worker wrote that state
    and converges on its resync), ``recycling`` (departing, converging by
    old-life-gone + fresh capacity), or ``stale`` (a quiet row past the freshness
    bound — reconnecting or dead, carrying no convergence promise). A decayed row is
    ``stale`` regardless of its written state."""

    applied = "applied"
    failed = "failed"
    missing = "missing"
    departed = "departed"
    timed_out = "timed_out"
    resyncing = "resyncing"
    recycling = "recycling"
    stale = "stale"


class WorkerKind(StrEnum):
    """The two worker kinds that join the bus."""

    serve = "serve"
    backend = "backend"


class WorkerState(StrEnum):
    """The lifecycle state a worker advertises in its presence value.

    ``resyncing`` while its boot/reconnect resync runs, ``ready`` once converged,
    ``recycling`` once it has begun a graceful self-exit."""

    ready = "ready"
    resyncing = "resyncing"
    recycling = "recycling"


class LastOp(BaseModel):
    """The last op a worker applied, stamped into presence after its terminal reply."""

    op: str
    outcome: str
    at: str


class WorkerIdentity(BaseModel):
    """This process's bus identity for the life of one held slot claim.

    ``name`` is a slot ``{kind}-{n}`` (the lowest free ordinal), ``generation`` the
    monotonic life counter minted with the claim. ``member`` is ``False`` only for a
    fork child's derived non-member identity (it claims and registers nothing); it is
    a process-local flag, never serialized onto the wire or into presence."""

    name: str
    kind: WorkerKind
    pid: int
    generation: int
    member: bool = Field(default=True, exclude=True)


class WorkerRow(BaseModel):
    """One presence row on the census: the full advertised worker state.

    ``pttl_ms`` is the raw remaining PTTL captured ALONGSIDE the value at scan time —
    an internal freshness measurement, excluded from ``model_dump`` so it never leaks
    into an API payload, and never part of the presence value on Redis. Staleness is
    NOT stored here: it is computed from ``pttl_ms`` by :func:`presence_fresh`."""

    name: str
    kind: WorkerKind
    pid: int
    generation: int
    joined_at: str
    beat_at: str
    state: WorkerState
    last_op: LastOp | None = None
    pttl_ms: int | None = Field(default=None, exclude=True)


def presence_fresh(pttl_ms: int | None, heartbeat_ttl: float) -> bool:
    """Whether a presence row's remaining PTTL clears the freshness bound.

    The SOLE home of the ``ttl - 2*interval`` bound. The heartbeat refreshes the key
    every ``ttl/3``, so a remaining PTTL at or below ``ttl - 2*(ttl/3) = ttl/3`` means
    the last beat was over two intervals ago. Clock-independent — it reads the raw
    redis PTTL, never a worker-stamped ``beat_at`` against the reader's clock. A row
    with no measured PTTL (absent/expired between scan and read) is not fresh."""
    if pttl_ms is None:
        return False
    interval = heartbeat_ttl / 3
    bound_ms = (heartbeat_ttl - 2 * interval) * 1000
    return pttl_ms > bound_ms


class _PresenceValue(BaseModel):
    """The in-memory presence-state object the bus owns while subscribed.

    ALL presence writes (heartbeat ``beat_at``, state transitions, the ``last_op``
    stamp) mutate this ONE object and serialize it whole on every SET — no writer
    reconstructs the value from a redis read, so independent writers never silently
    drop each other's fields. ``name`` lives in the presence KEY, not this value."""

    kind: WorkerKind
    pid: int
    generation: int
    joined_at: str
    beat_at: str
    state: WorkerState
    last_op: LastOp | None = None


class LocalApplyResult(BaseModel):
    """The publisher's own already-completed self-apply outcome, handed to
    :meth:`WorkerBus.publish` so the bus can synthesize a truthful self entry.

    ``outcome`` is terminal — ``applied`` on success, ``failed`` (with ``error``
    attached) on the publish-anyway path where the broadcast still goes out after a
    failed local apply. ``payload`` rides the same optional shape as a wire reply so
    the serving worker's own query data appears in a query op's fleet result."""

    outcome: OpOutcome
    payload: Any | None = None
    error: str | None = None

    @field_validator("outcome")
    @classmethod
    def _terminal_only(cls, value: OpOutcome) -> OpOutcome:
        if value not in (OpOutcome.applied, OpOutcome.failed):
            raise ValueError(
                "LocalApplyResult.outcome must be 'applied' or 'failed' — the caller's own terminal result"
            )
        return value


class WorkerResult(BaseModel):
    """One worker's outcome within a :class:`FleetResult`.

    ``payload`` carries query-op data (a read rides the same fan-out shape as a
    mutation); ``error`` carries a failed apply's message; ``detail`` carries the
    publisher's report text for a computed ``missing``/``departed``/``timed_out``."""

    name: str
    outcome: OpOutcome
    payload: Any | None = None
    error: str | None = None
    detail: str | None = None


class FleetResult(BaseModel):
    """The awaited result of one :meth:`WorkerBus.publish`.

    Two honest shapes. Reachable (``reachable=True``): ``results`` holds, per worker,
    the expected workers' verdicts (the synthesized self entry included) AND the gap
    rows (``resyncing`` / ``recycling`` / ``stale``) for rows that failed the ready+
    fresh gate — each carried as its actual condition rather than dropped.
    Bus-unreachable (``reachable=False``): the transport failed before any worker
    could reply, so there is NO worker list — only ``error``. ``local_only`` marks
    the result of the no-op :meth:`WorkerBus.local` variant."""

    op: str
    reachable: bool = True
    local_only: bool = False
    results: list[WorkerResult] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the bus was reachable and every worker applied."""
        return self.reachable and all(r.outcome == OpOutcome.applied for r in self.results)


def _decode(raw: Any) -> dict[str, Any] | None:
    """Decode a wire frame to a dict, or ``None`` (logged) when it is not an object."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("worker bus: discarding non-JSON wire frame", exc_info=True)
        return None
    if not isinstance(data, dict):
        logger.warning("worker bus: discarding non-object wire frame: %r", data)
        return None
    return data


def _merge_terminal(terminal: dict[str, WorkerResult], name: str, result: WorkerResult) -> None:
    """Fold one terminal reply into the per-worker map, worst-outcome-wins.

    A terminal reply supersedes nothing but another terminal; among terminals a
    failure is never overridden by a later same-worker success, so a genuinely
    failed apply cannot be masked."""
    existing = terminal.get(name)
    if existing is not None and existing.outcome == OpOutcome.failed:
        return
    terminal[name] = result


class UnknownFleetTargetsError(ValueError):
    """A fleet op named target workers absent from the presence census.

    A caller-side bad-request error (a typo'd or departed worker name), raised
    BEFORE any side effect; publishers surface it as a 400.
    """
