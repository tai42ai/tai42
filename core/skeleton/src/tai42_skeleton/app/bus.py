"""The worker bus — the app's one INTERNAL fleet fan-out primitive.

Every process (HTTP server or backend runtime) that shares a manifest joins one
Redis pub/sub control channel through :meth:`WorkerBus.subscribe` and keeps a TTL
presence key alive; a mutation reaches the whole fleet through the single awaited
:meth:`WorkerBus.publish`, which collects a per-worker outcome from every live
worker.

This is app-owned internal infrastructure, like the reload gate — NOT a plugin.
Nothing here is registrable, swappable, or user-selectable; there is exactly one
bus, and no manifest field chooses an implementation. A deployment with no Redis
configured runs on :meth:`WorkerBus.local`, the no-op variant.

Identity
--------
A worker is a named SLOT: ``{kind}-{n}`` (``serve-1``, ``backend-2``), the lowest
free ordinal, claimed atomically in bus Redis at subscribe time with ``SET NX`` on
the claim key and a per-name ``INCR`` generation counter. The claim carries a
per-process token (uuid4); a heartbeat renews it with a compare-token Lua script,
and the presence write follows ONLY on a successful renew. A renew miss (token
mismatch or absent key) is a LOST slot: the worker abandons the identity and
re-mints a NEW life (fresh claim + ``INCR``, possibly a different name) — a lost
slot is never resumed. Name and generation are minted once per life and immutable
for the life of the held claim; a reconnect that still holds its claim keeps them.

Namespacing
-----------
``TAI_BUS_NAMESPACE`` (default ``tai``) prefixes the control channel, every reply
channel, and every presence/slot/generation key. Redis pub/sub is server-global (NOT
scoped by db index), so two deployments/stacks sharing one Redis MUST diverge by
namespace or they would cross-talk; a shared-Redis deployment sets a unique
namespace per stack.

Transport shape
---------------
One control channel carries every op; each op names an ephemeral reply channel for
its confirmations and stamps its ``op_id`` (the reply-channel uuid). Every op frame
carries the publisher's ``(name, generation)``; both reply frames carry the
responder's ``(name, generation)`` and the echoed ``op_id``. ``_collect`` keys the
expected set on ``(name, generation)`` and DISCARDS a reply whose generation or
op_id does not match — a worker that lost its slot but has not yet discovered it (up
to one heartbeat of detection lag) can still reply under its stale life; the
generation gate drops exactly those. The census is a scan of the per-name presence
keys. Two wire messages come back from a subscriber per op: a ``received`` ack the
instant the op is delivered and exactly one terminal ``applied``/``failed`` once the
op has fully applied. ``timed_out`` and ``departed`` are never wire replies — the
publisher computes them from the presence census at the report cut.

Presence lifecycle
------------------
Presence is written under ``{ns}:bus:presence:{name}`` with a value carrying
``{kind, pid, generation, joined_at, beat_at, state, last_op?}`` and refreshed at
``ttl/3``. It is NOT force-deleted on a reconnect: the TTL carries the row across a
blip so a reader can tell "same worker, still here on TTL" from "new life". A
transport-error or lost-slot exit leaves the keys to their TTL; only a deliberate
stop deletes the presence row, and only after the compare-token claim release
succeeds (an absent/foreign claim means the slot already belongs to a new holder,
whose row must not be deleted). The row's ``state`` is ``resyncing`` before the boot
resync, ``ready`` after, and ``recycling`` before a graceful self-exit.

Fork safety
-----------
A forked child (an rq work-horse per job, a celery prefork pool child per task)
inherits the parent's :class:`WorkerBus` object across ``os.fork()``. The bus
registers an ``os.register_at_fork`` after-in-child hook that re-derives the child's
identity to an explicit NON-MEMBER: ``{parent-name}/fork-{pid}`` generation 0, which
claims nothing, registers no presence, and can never collide with a member — so a
fleet op the child publishes is applied by the parent instead of echo-skipped as the
parent's own broadcast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from tai42_contract.errors import ClientDisconnectedError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus_settings import BusSettings
from tai42_skeleton.utils.redis_typing import eval_script

logger = logging.getLogger(__name__)

# Wire fields that address/route an op rather than describe it; stripped before the
# op payload is handed to the subscriber callback.
_TRANSPORT_KEYS = frozenset({"name", "generation", "op_id", "reply_to", "targets"})

# How long one idle pub/sub poll blocks in the subscribe loop; bounds the latency
# of noticing a cancellation and of the next presence refresh.
_POLL_TIMEOUT = 0.1

# Compare-token renew: extend the claim TTL only while this process still holds the
# token, in one atomic step. The check-then-act GET/SET split cannot guarantee one
# live owner — a stall between the GET and the write can outlive the TTL, a new
# claimant wins via SET NX, and the delayed write would clobber it.
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# Compare-token release: delete the claim key only while this process still holds
# the token, so a slot already retaken by a new holder is never released out from
# under it.
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

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


class WorkerBus:
    """The one app-owned internal fan-out primitive over Redis pub/sub.

    Bound to this process's kind + pid at construction; its slot ``name`` and
    ``generation`` are minted on the first successful claim at subscribe time and
    then IMMUTABLE for the life of the held claim (a lost claim ends that life and
    re-mints a new one). :meth:`publish` reads the one identity to echo-skip the
    publisher's own broadcast and synthesize the self entry; the subscriber, presence
    writer, and echo-skip all read the SAME attribute, so the two-holder drift is
    structurally impossible. Constructed once per process by the lifecycle."""

    def __init__(
        self,
        settings: BusSettings,
        *,
        kind: WorkerKind,
        pid: int | None = None,
        local: bool = False,
        reconnect_backoff_initial: float = 0.5,
        reconnect_backoff_max: float = 30.0,
        reconnect_backoff_factor: float = 2.0,
    ) -> None:
        self._settings = settings
        self._kind = kind
        self._pid = pid if pid is not None else os.getpid()
        self._local = local
        self._backoff_initial = reconnect_backoff_initial
        self._backoff_max = reconnect_backoff_max
        self._backoff_factor = reconnect_backoff_factor
        # A per-process claim token minted once at construction; the claim key is won
        # by ``SET <token> NX`` and only this token may renew or release it.
        self._claim_token = uuid.uuid4().hex
        # Minted on the first successful claim (subscribe time); ``None`` until then
        # for a real bus. The busless variant self-mints below.
        self._identity: WorkerIdentity | None = None
        self._joined_at: str | None = None
        # The in-memory presence-state object, alive only while subscribed.
        self._presence: _PresenceValue | None = None
        # Set in a fork child of a member parent that had not yet claimed: it has no
        # name to derive a non-member identity from, and an in-hook raise is
        # unraisable, so any later bus use raises loudly at the use site instead.
        self._poisoned = False
        # Single-shot post-terminal-reply slot: an op handler arms it during its
        # callback, and the subscription loop fires it AFTER the op's terminal reply
        # ships (recycle's graceful self-exit). At most one op is in flight on the
        # single subscription task, so there is never more than one armed action.
        self._post_reply_slot: Callable[[], None] | None = None
        if local:
            # The busless variant self-mints a full member identity at construction —
            # no claim, no INCR, no presence, no heartbeat (there is no connection).
            self._identity = WorkerIdentity(name=f"{kind.value}-1", kind=kind, pid=self._pid, generation=1)
            self._joined_at = _utcnow_iso()
        else:
            # Fork safety: a forked child inherits this bus object, so it re-derives its
            # own non-member identity in the child (see _fork_child_nonmember). One
            # registration per real bus instance; the bound hook is inherited across
            # further forks, so a re-derived child that forks again re-derives.
            os.register_at_fork(after_in_child=self._fork_child_nonmember)

    def _fork_child_nonmember(self) -> None:
        """Re-derive this bus's identity to an explicit NON-MEMBER in a forked child
        (an ``os.register_at_fork`` after-in-child hook registered at construction).

        A child inherits the parent's ``WorkerBus`` — identity included — across
        ``os.fork()``. Left shared, a fleet op the child publishes would carry the
        PARENT's identity and be echo-skipped by the parent's own subscription. A
        derived name ``{parent-name}/fork-{pid}`` at generation 0 makes the child's
        published op a foreign identity the parent applies instead of skipping, and
        can never collide with a member: the child claims nothing, registers no
        presence, and its inherited subscription task stays dormant.

        The name is derived ONLY from a parent that HAS an identity (a member parent
        post-claim, or a local bus). A member parent that has not yet claimed has no
        name to derive from — and a raise here is unraisable — so the identity is
        POISONED instead: any bus use in the child raises loudly at the use site."""
        parent = self._identity
        if parent is None:
            self._poisoned = True
            self._identity = None
            return
        self._identity = WorkerIdentity(
            name=f"{parent.name}/fork-{os.getpid()}",
            kind=parent.kind,
            pid=os.getpid(),
            generation=0,
            member=False,
        )

    @classmethod
    def local(cls, kind: WorkerKind = WorkerKind.serve) -> WorkerBus:
        """The no-op variant for a single-worker / file-mode / no-backend / no-bus
        process: :meth:`publish` returns a local-only result, :meth:`subscribe`
        parks, :meth:`census` returns just this process's one synthesized row. Legal
        only under the boot rules that permit a busless deployment. The ``{kind}-1``
        generation-1 identity is self-minted at construction."""
        return cls(BusSettings(), kind=kind, local=True)

    @property
    def heartbeat_ttl(self) -> float:
        """This bus's presence-key TTL, the freshness cadence :func:`presence_fresh`
        gates against — the ONE bound a stale check reads, so no consumer hardcodes a
        threshold of its own."""
        return self._settings.heartbeat_ttl

    @property
    def identity(self) -> WorkerIdentity:
        """This process's bus identity. Raises before the slot is claimed (a real bus
        pre-subscribe), or in a fork child of an unclaimed member parent (a poisoned
        identity) — never a silent ``None``/placeholder name on the wire."""
        if self._poisoned:
            raise RuntimeError(
                "worker bus: forked from a member parent that had not yet claimed a slot — "
                "no name to derive a non-member identity from"
            )
        identity = self._identity
        if identity is None:
            raise RuntimeError("worker bus: identity is not minted yet — the slot has not been claimed")
        return identity

    def arm_post_reply(self, action: Callable[[], None]) -> None:
        """Arm the single-shot slot the subscription loop fires AFTER the current op's
        terminal reply ships and only on a clean apply (recycle's post-reply self-exit).
        Called from an op handler during its callback; the last arming wins."""
        self._post_reply_slot = action

    async def mark_recycling(self) -> None:
        """Write ``state=recycling`` into presence before a graceful self-exit, so the
        census shows WHY this worker is departing rather than reading as merely quiet.

        Renew-gated like every presence write (the compare-token renew runs FIRST and
        the write is skipped on a miss) AND best-effort against transport errors: a
        cosmetic census write can never abort the recycle it precedes, so a blip on the
        connect/renew/set is logged and swallowed rather than propagated. A no-op when
        this bus holds no presence (a busless or unsubscribed bus)."""
        presence = self._presence
        if presence is None:
            return
        name = self.identity.name
        try:
            async with client_ctx(RedisClient, self._settings.redis) as conn:
                r: Any = conn
                if not await self._renew_claim(r, name):
                    return
                presence.state = WorkerState.recycling
                presence.beat_at = _utcnow_iso()
                await self._set_presence(r, self._settings.presence_key(name), presence)
        except _TRANSPORT_ERRORS:
            logger.warning(
                "worker bus: recycling-state write for %s failed — census may briefly read it as quiet; "
                "proceeding with recycle",
                name,
                exc_info=True,
            )

    # -- Publisher side --------------------------------------------------------

    async def publish(
        self,
        op: dict[str, Any],
        targets: list[str] | None,
        local: LocalApplyResult | None,
    ) -> FleetResult:
        """Broadcast one op to the fleet and collect a per-worker outcome; awaited.

        ``targets=None`` is the whole fleet. ``local`` is the caller's own
        already-completed self-apply outcome — the bus cannot truthfully report the
        publisher's own name otherwise. The two are validated against ``targets``
        BOTH ways: ``local=None`` with the publisher targeted (``targets=None`` or a
        self-including list) raises (the publisher would be an expected worker that
        can never reply), and ``local`` supplied with self-EXCLUDING targets raises
        (a self entry for an unexpected worker would be a false report).

        Absent targets are NOT re-raised here — a targeted publisher validates first
        via :meth:`validate_targets`, so a target that vanished between validation
        and here is churn, reported honestly as ``departed`` rather than raised."""
        op_name = self._op_name(op)
        self._validate_local_targets(local, targets)

        if self._local:
            return self._local_publish(op_name, targets, local)

        try:
            fleet = await self._broadcast(op_name, op, targets)
        except _TRANSPORT_ERRORS as exc:
            logger.error("worker bus: publish of %r failed — bus unreachable", op_name, exc_info=True)
            return FleetResult(op=op_name, reachable=False, error=f"{type(exc).__name__}: {exc}")

        if local is not None:
            fleet.append(self._self_result(local))
        fleet.sort(key=lambda r: r.name)
        return FleetResult(op=op_name, results=fleet)

    async def _broadcast(self, op_name: str, op: dict[str, Any], targets: list[str] | None) -> list[WorkerResult]:
        op_id = uuid.uuid4().hex
        reply_channel = f"{self._settings.reply_prefix}{op_id}"
        identity = self.identity
        async with client_ctx(RedisClient, self._settings.redis) as conn:
            r: Any = conn
            expected, gaps = await self._classify_workers(r, targets)
            # ``r.pubsub()`` is a non-I/O constructor, bound BEFORE the try so pubsub is
            # never unbound in the finally. A raising subscribe still reaches the finally,
            # which unsubscribes then ALWAYS closes the pub/sub (the aclose runs even if
            # the unsubscribe raises) — no leaked connection on a failed join.
            pubsub = r.pubsub()
            try:
                await pubsub.subscribe(reply_channel)
                wire = {
                    **op,
                    "name": identity.name,
                    "generation": identity.generation,
                    "op_id": op_id,
                    "reply_to": reply_channel,
                    "targets": targets,
                }
                await r.publish(self._settings.channel, json.dumps(wire))
                collected = await self._collect(r, pubsub, expected, gaps, op_id, op_name)
            finally:
                try:
                    await pubsub.unsubscribe(reply_channel)
                finally:
                    await pubsub.aclose()
        return list(collected.values())

    async def _classify_workers(
        self, r: Any, targets: list[str] | None
    ) -> tuple[dict[str, int | None], dict[str, WorkerResult]]:
        """Split the live fleet into the EXPECTED set (keyed name → generation, awaited
        for a reply) and the GAP set (keyed name → its actual-condition result).

        A gap row fails the ready+fresh gate, so it is not an expected worker; rather
        than being dropped it is landed as its own condition (``resyncing`` /
        ``recycling`` / ``stale``). Whole-fleet: expected = the READY rows past the
        freshness gate, minus self; every other row is a gap. Targeted: each named
        target minus self — a ready+fresh target is expected, a target present-but-gap
        carries its gap outcome, and a target absent from the census stays expected with
        generation ``None`` so it is reported ``departed`` at the cut."""
        rows = await self._scan_workers(r)
        by_name = {row.name: row for row in rows}
        self_name = self.identity.name
        expected: dict[str, int | None] = {}
        gaps: dict[str, WorkerResult] = {}
        if targets is None:
            for name, row in by_name.items():
                if name == self_name:
                    continue
                if self._is_ready_fresh(row):
                    expected[name] = row.generation
                else:
                    gaps[name] = self._gap_result(row)
            return expected, gaps
        # Targets are NOT re-validated here (the caller validated); an absent target is
        # reported as departed at the cut, a present-but-gap target as its gap outcome.
        for name in targets:
            if name == self_name:
                continue
            row = by_name.get(name)
            if row is None:
                expected[name] = None
            elif self._is_ready_fresh(row):
                expected[name] = row.generation
            else:
                gaps[name] = self._gap_result(row)
        return expected, gaps

    def _is_ready_fresh(self, row: WorkerRow) -> bool:
        """The ready+fresh gate: a row is an expected worker only when it advertises
        ``ready`` AND its captured PTTL clears the freshness bound."""
        return row.state == WorkerState.ready and presence_fresh(row.pttl_ms, self._settings.heartbeat_ttl)

    def _gap_result(self, row: WorkerRow) -> WorkerResult:
        """Classify a gap row (one that fails the ready+fresh gate) as its actual
        condition. A decayed row is ``stale`` regardless of its written state — a worker
        that died mid-resync/mid-recycle carries no convergence promise; a fresh
        resyncing/recycling row keeps its written state as the outcome."""
        if not presence_fresh(row.pttl_ms, self._settings.heartbeat_ttl):
            outcome = OpOutcome.stale
        elif row.state == WorkerState.resyncing:
            outcome = OpOutcome.resyncing
        elif row.state == WorkerState.recycling:
            outcome = OpOutcome.recycling
        else:
            outcome = OpOutcome.stale
        age = _beat_age_seconds(row.beat_at)
        detail = f"state={row.state.value}" + (f", last beat {age:.0f}s ago" if age is not None else "")
        return WorkerResult(name=row.name, outcome=outcome, detail=detail)

    async def _collect(
        self,
        r: Any,
        pubsub: Any,
        expected: dict[str, int | None],
        gaps: dict[str, WorkerResult],
        op_id: str,
        op_name: str,
    ) -> dict[str, WorkerResult]:
        terminal: dict[str, WorkerResult] = {}
        acked: set[str] = set()
        loop = asyncio.get_running_loop()
        start = loop.time()
        ack_deadline = start + self._settings.ack_timeout
        apply_deadline = start + self._settings.apply_timeout
        ack_checked = False
        transport_error: str | None = None

        # Early exit counts TERMINAL wire replies only; a provisional missing/departed
        # never enables it, so an in-flight ``applied`` can never be cut off early.
        while expected.keys() - terminal.keys():
            now = loop.time()
            if now >= apply_deadline:
                break
            if not ack_checked and now >= ack_deadline:
                # Provisional verdicts at the ack deadline are diagnostic only; the
                # finalize pass below re-checks presence, so they never block a
                # later terminal reply. Marking the pass done is what matters here.
                ack_checked = True
            next_deadline = apply_deadline if ack_checked else ack_deadline
            timeout = max(0.01, next_deadline - now)
            try:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            except _TRANSPORT_ERRORS as exc:
                # A blip mid-collection is reported loudly on the affected workers
                # (below), never silently dropped.
                transport_error = f"{type(exc).__name__}: {exc}"
                logger.error("worker bus: reply collection for %r hit a transport error", op_name, exc_info=True)
                break
            if msg is None:
                continue
            frame = _decode(msg["data"])
            if frame is None:
                continue
            name = frame.get("name")
            if name not in expected:
                continue
            expected_gen = expected[name]
            got_gen = frame.get("generation")
            # generation is the life-binding, op_id the op-binding: a worker that lost
            # its slot but has not yet discovered it (up to one heartbeat of detection
            # lag) can reply under a stale life — discard those loudly, never fold.
            if expected_gen is not None and got_gen != expected_gen:
                logger.warning(
                    "worker bus: discarding reply from %s — expected generation %s, got %s (op_id %s)",
                    name,
                    expected_gen,
                    got_gen,
                    frame.get("op_id"),
                )
                continue
            if frame.get("op_id") != op_id:
                logger.warning(
                    "worker bus: discarding reply from %s — op_id mismatch (expected %s, got %s)",
                    name,
                    op_id,
                    frame.get("op_id"),
                )
                continue
            phase = frame.get("phase")
            if phase == "received":
                acked.add(name)
            elif phase == "terminal":
                _merge_terminal(terminal, name, self._terminal_result(name, frame))

        results: dict[str, WorkerResult] = {}
        for name in expected:
            verdict = terminal.get(name)
            if verdict is None:
                verdict = await self._computed_verdict(r, name, expected[name], name in acked, transport_error)
            results[name] = verdict
        # Gap rows (failing the ready+fresh gate) were never expected to reply; land
        # each as its own actual condition (resyncing/recycling/stale) rather than
        # dropping it. Disjoint from ``expected`` by construction.
        results.update(gaps)
        return results

    def _terminal_result(self, name: str, frame: dict[str, Any]) -> WorkerResult:
        raw = frame.get("outcome")
        outcome = OpOutcome.failed if raw == OpOutcome.failed.value else OpOutcome.applied
        return WorkerResult(name=name, outcome=outcome, payload=frame.get("payload"), error=frame.get("error"))

    async def _computed_verdict(
        self, r: Any, name: str, expected_gen: int | None, acked: bool, transport_error: str | None
    ) -> WorkerResult:
        """Classify a worker that holds no terminal reply, re-checking presence.

        Generation-aware: when the presence key now shows a generation GREATER than the
        one expected at publish, the target's life ended and a replacement took its slot
        mid-apply — ``departed`` (the reply gate already discarded the stale life's
        replies). An absent key is ``departed``; a still-live same-life key is
        ``timed_out`` (acked) or ``missing`` (never acked)."""
        status, current_gen = await self._recheck_presence(r, name)
        if status == "alive" and expected_gen is not None and current_gen is not None and current_gen > expected_gen:
            return WorkerResult(
                name=name,
                outcome=OpOutcome.departed,
                detail=f"replaced by generation {current_gen} mid-apply",
            )
        absent = status == "absent"
        if acked:
            if absent:
                return WorkerResult(
                    name=name,
                    outcome=OpOutcome.departed,
                    detail="acked then its presence key expired before applying — worker departed mid-apply",
                )
            detail = "acked but did not apply within the apply timeout — op delivered, verify via fleet census/reload"
            if transport_error is not None:
                detail = f"{detail} (reply collection transport error: {transport_error})"
            return WorkerResult(name=name, outcome=OpOutcome.timed_out, detail=detail)
        if absent:
            return WorkerResult(
                name=name,
                outcome=OpOutcome.departed,
                detail="no reply and presence key expired — worker departed",
            )
        detail = "no received-ack within the ack timeout while presence stayed live — worker missing (alive but silent)"
        if transport_error is not None:
            detail = f"{detail} (reply collection transport error: {transport_error})"
        return WorkerResult(name=name, outcome=OpOutcome.missing, detail=detail)

    async def _recheck_presence(self, r: Any, name: str) -> tuple[str, int | None]:
        """Re-read a worker's presence key at the report cut, returning ``("absent",
        None)`` when the key is gone, ``("alive", generation)`` when it is present (the
        generation parsed from the value, ``None`` if the value does not parse), or
        ``("unreachable", None)`` when the check itself could not reach Redis (the caller
        degrades to a loud missing/timed_out). The generation lets the verdict see a
        replacement that took the slot mid-apply."""
        try:
            raw = await r.get(self._settings.presence_key(name))
        except _TRANSPORT_ERRORS:
            logger.error("worker bus: presence re-check for %s failed", name, exc_info=True)
            return ("unreachable", None)
        if raw is None:
            return ("absent", None)
        try:
            text = raw.decode() if isinstance(raw, bytes) else raw
            generation = int(json.loads(text)["generation"])
        except (ValueError, TypeError, KeyError):
            logger.warning("worker bus: presence value for %s is unparseable at the report cut", name, exc_info=True)
            return ("alive", None)
        return ("alive", generation)

    def _self_result(self, local: LocalApplyResult) -> WorkerResult:
        return WorkerResult(
            name=self.identity.name,
            outcome=local.outcome,
            payload=local.payload,
            error=local.error,
        )

    def _local_publish(self, op_name: str, targets: list[str] | None, local: LocalApplyResult | None) -> FleetResult:
        if targets is not None:
            unknown = sorted(set(targets) - {self.identity.name})
            if unknown:
                raise UnknownFleetTargetsError(
                    f"worker bus (local): cannot reach targets not on this process: {unknown}"
                )
        results = [self._self_result(local)] if local is not None else []
        return FleetResult(op=op_name, local_only=True, results=results)

    def _validate_local_targets(self, local: LocalApplyResult | None, targets: list[str] | None) -> None:
        # A non-member (a fork child) is NEVER a fleet target: its own name is not on
        # the census, so it can neither be self-targeted nor carry a truthful self entry.
        self_targeted = self.identity.member and (targets is None or self.identity.name in targets)
        if local is None and self_targeted:
            raise ValueError(
                "publish: local=None but the publisher is a targeted worker (targets=None or self-including); "
                "the publisher would be an expected worker that can never reply"
            )
        if local is not None and not self_targeted:
            raise ValueError(
                "publish: a local result was supplied but targets exclude the publisher; "
                "a self entry for an unexpected worker would be a false report"
            )

    @staticmethod
    def _op_name(op: dict[str, Any]) -> str:
        name = op.get("op")
        if not isinstance(name, str) or not name:
            raise ValueError("publish: op dict must carry a non-empty 'op' name")
        return name

    # -- Census + target validation --------------------------------------------

    async def census(self) -> list[WorkerRow]:
        """The live fleet: one :class:`WorkerRow` per registered presence key.

        This is the fleet worker listing (it backs ``GET /api/fleet/workers``). The
        busless variant returns its ONE synthesized ready row, its ``beat_at``
        computed fresh at call time (never frozen at construction)."""
        if self._local:
            return [self._local_row()]
        async with client_ctx(RedisClient, self._settings.redis) as conn:
            return await self._scan_workers(conn)

    def _local_row(self) -> WorkerRow:
        identity = self.identity
        joined_at = self._joined_at if self._joined_at is not None else _utcnow_iso()
        return WorkerRow(
            name=identity.name,
            kind=identity.kind,
            pid=identity.pid,
            generation=identity.generation,
            joined_at=joined_at,
            beat_at=_utcnow_iso(),
            state=WorkerState.ready,
            last_op=None,
            # The lone busless worker is this live process — always fresh, so it clears
            # the same freshness predicate every other row is judged by (never stale).
            pttl_ms=int(self._settings.heartbeat_ttl * 1000),
        )

    async def validate_targets(self, targets: list[str] | None) -> None:
        """Raise naming any target absent from the census — a caller-side seam run
        BEFORE the caller's local apply, so validation precedes side effects.

        ``targets=None`` (whole fleet) is always valid. A typo'd worker name is an
        error here, never a silent narrowing at publish time."""
        if targets is None:
            return
        # Seed self into the live set ONLY for a member: a non-member's own name
        # ({parent}/fork-{pid}) is not a fleet target, so naming it must be unknown.
        live = {self.identity.name} if self.identity.member else set()
        if not self._local:
            live |= {row.name for row in await self.census()}
        unknown = sorted(set(targets) - live)
        if unknown:
            raise UnknownFleetTargetsError(f"worker bus: unknown fleet targets (not on the census): {unknown}")

    async def _scan_workers(self, r: Any) -> list[WorkerRow]:
        prefix = self._settings.presence_prefix
        names: list[str] = []
        keys: list[str] = []
        async for key in r.scan_iter(match=self._settings.presence_pattern):
            key_str = key.decode() if isinstance(key, bytes) else key
            names.append(key_str[len(prefix) :])
            keys.append(key_str)
        if not keys:
            return []
        # One round-trip for every scanned key: each key's GET and PTTL are queued
        # ADJACENT in a single pipeline, so the remaining PTTL is captured ALONGSIDE the
        # value (the freshness gate stays clock-independent, never a worker-stamped
        # beat_at against our clock) without a sequential read per worker.
        pipe = r.pipeline(transaction=False)
        for key_str in keys:
            pipe.get(key_str)
            pipe.pttl(key_str)
        outcomes = await pipe.execute()
        rows: list[WorkerRow] = []
        for i, name in enumerate(names):
            raw = outcomes[2 * i]
            if raw is None:
                # Expired between the scan and the read — no longer live, skipped.
                continue
            rows.append(self._row_from_presence(name, raw, outcomes[2 * i + 1]))
        return rows

    @staticmethod
    def _row_from_presence(name: str, raw: Any, pttl_ms: Any) -> WorkerRow:
        text = raw.decode() if isinstance(raw, bytes) else raw
        meta = json.loads(text)
        last_op_raw = meta.get("last_op")
        return WorkerRow(
            name=name,
            kind=WorkerKind(meta["kind"]),
            pid=int(meta["pid"]),
            generation=int(meta["generation"]),
            joined_at=meta["joined_at"],
            beat_at=meta["beat_at"],
            state=WorkerState(meta["state"]),
            last_op=LastOp(**last_op_raw) if last_op_raw is not None else None,
            pttl_ms=int(pttl_ms) if pttl_ms is not None else None,
        )

    # -- Subscriber side -------------------------------------------------------

    async def subscribe(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[Any]],
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Consume the control channel until cancelled, reconnecting with backoff.

        Claims a slot (``{kind}-{n}``, lowest free ordinal) and registers a TTL
        presence key for it, refreshed at ``ttl/3`` under a compare-token renew. Each
        op is applied by awaiting ``callback`` on this task, with a two-phase
        confirmation: a ``received`` ack the instant the op is delivered, then one
        terminal ``applied``/``failed`` when the callback returns (a raising callback
        ⇒ ``failed``). The publisher's own broadcast is echo-skipped by ``(name,
        generation)`` — its self entry is synthesized from the ``local`` result it
        hands :meth:`publish`.

        The claim is verified FIRST on every (re)connect: a held claim keeps the same
        name+generation across a reconnect; a lost claim (renew miss) re-mints a NEW
        life. The heartbeat starts BEFORE the ``on_ready`` resync (the resync budget
        exceeds the claim TTL, so an unrenewed claim would lapse mid-resync); presence
        is written ``resyncing`` before the resync and ``ready`` after, both under the
        renew-gated heartbeat path. The channel is subscribed before the resync, so an
        op broadcast during it is buffered and applied by the message loop. Presence is
        NOT force-deleted on a reconnect (its TTL carries the identity across a blip);
        only a deliberate stop deletes it, and only after the claim release succeeds.
        A lost slot is routed through a DEDICATED reconnect branch (never the
        transport-error path) that re-enters immediately with no backoff. Each
        transport reconnect attempt is ERROR-logged."""
        if self._local:
            await asyncio.Event().wait()
            return

        backoff = self._backoff_initial
        while True:
            established = False

            def _mark_established() -> None:
                nonlocal established
                established = True

            try:
                await self._run_subscription(callback, on_ready, _mark_established)
                return
            except asyncio.CancelledError:
                raise
            except SlotLostError as exc:
                # A lost slot on a (possibly) healthy connection is NOT a transport
                # outage: re-enter IMMEDIATELY with no backoff (the slot is gone,
                # nothing to wait out) and re-mint a new life on re-entry.
                logger.warning("worker bus: slot %s lost; re-minting a new life", exc)
                continue
            except _TRANSPORT_ERRORS:
                last = self._identity.name if self._identity is not None else self._kind.value
                logger.error(
                    "worker bus: subscription transport error for %s; reconnecting in %.2fs",
                    last,
                    backoff,
                    exc_info=True,
                )
            if established:
                backoff = self._backoff_initial
            await asyncio.sleep(backoff)
            backoff = min(backoff * self._backoff_factor, self._backoff_max)

    async def _run_subscription(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[Any]],
        on_ready: Callable[[], Awaitable[None]] | None,
        on_established: Callable[[], None],
    ) -> None:
        # ``fresh=True``: a DEDICATED, non-pooled connection for the process-lifetime
        # subscription, so an epoch retire's ``drain_epoch`` can never force-close it.
        # A pooled lease is stamped with the epoch current at acquire time; the
        # subscription holds it for the whole epoch, so every reload's retire would
        # force-close it (blocking the full drain budget first) → the subscription
        # reconnects → its ``on_ready`` fires a resync reload_config → which retires and
        # force-closes the NEXT lease → an unbounded reload cascade. The bus URL is
        # Tier-1 refused (never changes across a profile apply), so this connection is
        # invariant across epochs and has no reason to be epoch-scoped. A REAL bus outage
        # still raises a transport error here and drives the reconnect+resync self-heal.
        async with client_ctx(RedisClient, self._settings.redis, fresh=True) as conn:
            r: Any = conn
            pubsub = r.pubsub()
            await pubsub.subscribe(self._settings.channel)
            heartbeat: asyncio.Task[None] | None = None
            exit_kind = "transport"
            presence_key: str | None = None
            try:
                on_established()
                # Verify/claim the slot FIRST: a held claim keeps its name+generation
                # across the reconnect; a lost or absent claim mints a NEW life.
                await self._establish_identity(r)
                identity = self.identity
                presence_key = self._settings.presence_key(identity.name)
                # A fresh in-memory presence state: resyncing until on_ready converges.
                self._presence = _PresenceValue(
                    kind=identity.kind,
                    pid=identity.pid,
                    generation=identity.generation,
                    joined_at=self._joined_at if self._joined_at is not None else _utcnow_iso(),
                    beat_at=_utcnow_iso(),
                    state=WorkerState.resyncing,
                    last_op=None,
                )
                # Heartbeat FIRST (renew-gated), BEFORE on_ready: the resync budget
                # exceeds the claim TTL, so an unrenewed claim would lapse mid-resync.
                heartbeat = asyncio.create_task(
                    self._heartbeat_loop(r, presence_key),
                    name=f"tai-worker-bus-heartbeat-{identity.name}",
                )
                # Advertise ``resyncing`` before the resync, then ``ready`` after — both
                # renew-gated (skipped on a miss; the heartbeat's own miss raises
                # SlotLostError to route the re-mint). Presence is written BEFORE the
                # resync converges, so a mid-resync row is visible-but-excluded by the
                # freshness/state gate, not hidden. The channel is already subscribed, so
                # an op broadcast during the resync is buffered and applied below.
                await self._write_presence(r, presence_key, self._presence)
                if on_ready is not None:
                    await on_ready()
                self._presence.state = WorkerState.ready
                await self._write_presence(r, presence_key, self._presence)
                logger.info("worker bus: subscription live as %s", identity.name)
                while True:
                    if heartbeat.done():
                        # The heartbeat ended on its own — a presence refresh raised on
                        # a pooled command connection while this held pub/sub connection
                        # stayed healthy (an asymmetric transport failure), OR a renew
                        # miss raised SlotLostError (the slot was lost). Surface it and
                        # force the whole subscription down so the outer loop reconnects
                        # (transport) or re-mints (slot lost).
                        failure = self._heartbeat_failure(heartbeat, identity.name)
                        # Its exception is already retrieved and logged; drop the
                        # reference so teardown does not re-await and re-log it.
                        heartbeat = None
                        raise failure
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_POLL_TIMEOUT)
                    if msg is not None:
                        await self._handle_op(r, callback, msg)
            except asyncio.CancelledError:
                exit_kind = "deliberate"
                raise
            except SlotLostError:
                exit_kind = "slot_lost"
                raise
            except _TRANSPORT_ERRORS:
                exit_kind = "transport"
                raise
            finally:
                await self._stop_heartbeat(heartbeat)
                await self._teardown(r, pubsub, presence_key, exit_kind)

    async def _establish_identity(self, r: Any) -> None:
        """(Re)establish this process's claimed slot identity on a (re)connect.

        A held claim that still renews keeps its name+generation+joined_at across the
        reconnect (scoped to a never-lost claim). A lost or absent claim
        (renew miss on the old name, or no prior identity on first boot) mints a NEW
        life: the lowest free ``{kind}-{n}`` won by ``SET NX`` + a fresh ``INCR``."""
        if self._identity is not None and await self._renew_claim(r, self._identity.name):
            return
        self._identity = await self._claim_slot(r)
        self._joined_at = _utcnow_iso()

    async def _claim_slot(self, r: Any) -> WorkerIdentity:
        """Win the lowest free ``{kind}-{n}`` slot and mint its generation.

        ``SET <token> NX PX`` on the claim key claims the name atomically; the first
        free ordinal wins. ``INCR`` on the per-name generation counter mints a
        monotonic life number — never re-INCRed by a worker that cannot prove the
        claim (this runs ONLY on a fresh successful claim)."""
        ttl_ms = int(self._settings.heartbeat_ttl * 1000)
        n = 1
        while True:
            name = f"{self._kind.value}-{n}"
            won = await r.set(self._settings.slot_key(name), self._claim_token, nx=True, px=ttl_ms)
            if won:
                generation = int(await r.incr(self._settings.gen_key(name)))
                return WorkerIdentity(name=name, kind=self._kind, pid=self._pid, generation=generation)
            n += 1

    async def _renew_claim(self, r: Any, name: str) -> bool:
        """Extend the claim TTL iff this process still holds the token (one atomic
        compare-token Lua step). Returns False on a miss (token mismatch or absent
        key) — a lost slot."""
        px = int(self._settings.heartbeat_ttl * 1000)
        result = await eval_script(r, _RENEW_LUA, 1, self._settings.slot_key(name), self._claim_token, px)
        return bool(result)

    async def _release_claim(self, r: Any, name: str) -> bool:
        """Delete the claim key iff this process still holds the token (one atomic
        compare-token Lua step). Returns False on a miss — the slot already belongs to
        a new holder, so nothing was released."""
        result = await eval_script(r, _RELEASE_LUA, 1, self._settings.slot_key(name), self._claim_token)
        return bool(result)

    async def _heartbeat_loop(self, r: Any, presence_key: str) -> None:
        """Renew the claim then refresh presence at ``ttl/3`` on a task of its own.

        The compare-token renew runs FIRST on every beat; the presence write follows
        ONLY on a successful renew, so a life that cannot prove its claim never writes
        presence under that name. A renew miss RAISES :class:`SlotLostError`, which the
        message loop surfaces to force the re-mint. Liveness is decoupled from op apply
        on purpose: were the refresh folded into the single message loop, a callback
        that reloads for longer than the presence TTL would park the loop and let this
        worker's key EXPIRE while it is alive and applying — the census would drop the
        live worker. The callback's own awaits yield control, so this task renews and
        refreshes throughout a long apply."""
        interval = self._settings.heartbeat_ttl / 3
        while True:
            await asyncio.sleep(interval)
            name = self.identity.name
            # Renew FIRST; a miss is a lost slot. The presence write follows on the same
            # proven claim (no second renew), so it is never unguarded.
            if not await self._renew_claim(r, name):
                raise SlotLostError(name)
            presence = self._presence
            if presence is not None:
                presence.beat_at = _utcnow_iso()
                await self._set_presence(r, presence_key, presence)

    async def _write_presence(self, r: Any, presence_key: str, presence: _PresenceValue) -> None:
        """A renew-gated presence write: renew the claim FIRST, then serialize the
        whole in-memory value. Skipped on a renew miss (the heartbeat's own miss
        raises SlotLostError to re-mint), so no presence write is unguarded and an
        ex-owner in the loss-lag never regresses the census under its stale name."""
        if not await self._renew_claim(r, self.identity.name):
            return
        await self._set_presence(r, presence_key, presence)

    async def _set_presence(self, r: Any, presence_key: str, presence: _PresenceValue) -> None:
        await r.set(presence_key, presence.model_dump_json(), px=int(self._settings.heartbeat_ttl * 1000))

    def _heartbeat_failure(self, heartbeat: asyncio.Task[None], name: str) -> BaseException:
        """Turn a self-terminated heartbeat task into the failure that ends this
        subscription, logging it at ERROR immediately.

        A transport error (a lost pooled command connection) flows back into the
        reconnect-with-backoff loop; a :class:`SlotLostError` (a renew miss) flows into
        the dedicated re-mint branch. A heartbeat that returned without raising — it
        never should, its loop is unbounded — becomes a loud ``RuntimeError``."""
        exc = heartbeat.exception()
        if exc is None:
            exc = RuntimeError("worker bus: presence heartbeat returned without an error")
        logger.error("worker bus: presence heartbeat for %s stopped", name, exc_info=exc)
        return exc

    async def _stop_heartbeat(self, heartbeat: asyncio.Task[None] | None) -> None:
        """Cancel and await the presence-heartbeat task during teardown, so it never
        leaks past the subscription. A heartbeat that already died of a transport error
        (the dropped connection the subscription is reconnecting from) is logged loudly
        and swallowed so teardown still completes."""
        if heartbeat is None:
            return
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("worker bus: presence heartbeat task terminated abnormally", exc_info=True)

    async def _handle_op(
        self,
        r: Any,
        callback: Callable[[dict[str, Any]], Awaitable[Any]],
        msg: dict[str, Any],
    ) -> None:
        frame = _decode(msg["data"])
        if frame is None:
            return
        identity = self.identity
        if frame.get("name") == identity.name and frame.get("generation") == identity.generation:
            # Echo-skip by (name, generation): the publisher applied this op locally
            # before broadcasting and synthesizes its own self entry — delivering it
            # back would double-apply. A frame from an earlier life under the same name
            # (a different generation) is NOT ours and is applied normally.
            return
        targets = frame.get("targets")
        # Test presence, not truthiness: ``targets is None`` reaches every worker, a
        # list reaches exactly its members — so an empty list reaches nobody, matching
        # the publisher's empty expected set (no silent sibling apply).
        if targets is not None and identity.name not in targets:
            return
        reply_to = frame.get("reply_to")
        op_id = frame.get("op_id")
        responder = {"name": identity.name, "generation": identity.generation, "op_id": op_id}

        if reply_to:
            await self._reply(r, reply_to, {**responder, "phase": "received"})

        op_payload = {k: v for k, v in frame.items() if k not in _TRANSPORT_KEYS}
        applied = False
        outcome_value = OpOutcome.applied.value
        try:
            try:
                result = await callback(op_payload)
                terminal: dict[str, Any] = {**responder, "phase": "terminal", "outcome": OpOutcome.applied.value}
                if result is not None:
                    terminal["payload"] = result
                applied = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("worker bus: op %r failed on %s", op_payload.get("op"), identity.name, exc_info=True)
                outcome_value = OpOutcome.failed.value
                terminal = {
                    **responder,
                    "phase": "terminal",
                    "outcome": OpOutcome.failed.value,
                    "error": f"{type(exc).__name__}: {exc}",
                }

            if reply_to:
                await self._reply(r, reply_to, terminal)

            # Stamp the applied op into presence AFTER the terminal reply ships. This is
            # a renew-gated presence write like any other: skipped on a renew miss, so an
            # ex-owner in the loss-lag never regresses the census under its stale name.
            op_name = op_payload.get("op")
            if isinstance(op_name, str):
                await self._stamp_last_op(r, op_name, outcome_value)

            # Fire the armed post-reply slot (recycle's self-exit) only once the terminal
            # reply has shipped AND the op applied cleanly, so the publisher records the
            # applied outcome before this process departs. A handler may ONLY arm the slot:
            # scheduling the exit inside the callback would race the reply and be reported
            # ``timed_out``.
            if applied and self._post_reply_slot is not None:
                self._post_reply_slot()
        finally:
            # Single-shot: disarm on EVERY exit — clean fire, failed apply, a reply-publish
            # error, or cancellation — so no later op ever misfires a stale self-exit.
            self._post_reply_slot = None

    async def _stamp_last_op(self, r: Any, op_name: str, outcome: str) -> None:
        """Re-write presence with the last applied op (name + outcome + timestamp).

        A no-op when this bus holds no presence (not subscribed under a claimed slot).
        Renew-gated: the compare-token renew runs FIRST and the write is skipped on a
        miss — the re-mint itself rides the next heartbeat's ``SlotLostError``, never
        this message-loop write."""
        presence = self._presence
        if presence is None:
            return
        name = self.identity.name
        if not await self._renew_claim(r, name):
            return
        presence.last_op = LastOp(op=op_name, outcome=outcome, at=_utcnow_iso())
        presence.beat_at = _utcnow_iso()
        await self._set_presence(r, self._settings.presence_key(name), presence)

    async def _reply(self, r: Any, channel: str, frame: dict[str, Any]) -> None:
        await r.publish(channel, json.dumps(frame))

    async def _teardown(self, r: Any, pubsub: Any, presence_key: str | None, exit_kind: str) -> None:
        """Leave the channel and, per the exit kind, the presence + claim keys.

        Deliberate stop (unsubscribe / cancellation during ``stop()``): release the
        claim FIRST (compare-token DEL), then delete the presence row ONLY if the
        release succeeded — a foreign/absent claim means the slot already belongs to a
        new holder, whose row must not be deleted. Transport-error and lost-slot exits
        leave BOTH keys to their TTL (the identity is carried across the reconnect /
        the name already belongs to the new holder). The channel close is always
        attempted; best-effort, never silent."""
        if exit_kind == "deliberate" and self._identity is not None:
            # Release the claim whenever a held identity stops deliberately — even in
            # the claim window before the presence row is written (presence_key still
            # None), so a stop mid-claim never leaves the slot to its TTL. The presence
            # DELETE is gated on presence_key: there is no row to remove until it is set.
            # Transport-guarded: a deliberate stop is a CancelledError propagating through
            # this finally, so a release blip must be logged and the claim left to its TTL
            # (identical to a transport exit), never propagated — else it would replace the
            # CancelledError and reconnect-loop a stop that was meant to terminate.
            try:
                released = await self._release_claim(r, self._identity.name)
            except _TRANSPORT_ERRORS:
                logger.warning(
                    "worker bus: claim release for %s failed on deliberate stop (transport) — leaving it to TTL",
                    self._identity.name,
                    exc_info=True,
                )
            else:
                if released and presence_key is not None:
                    try:
                        await r.delete(presence_key)
                    except Exception:
                        logger.warning(
                            "worker bus: presence delete for %s failed (TTL will expire it)",
                            presence_key,
                            exc_info=True,
                        )
                elif not released and presence_key is not None:
                    logger.warning(
                        "worker bus: claim for %s was already reclaimed on stop — leaving its presence row untouched",
                        presence_key,
                    )
        elif exit_kind != "deliberate":
            logger.info("worker bus: %s exit — presence/claim keys left to their TTL across the reconnect", exit_kind)
        self._presence = None
        try:
            await pubsub.unsubscribe(self._settings.channel)
            await pubsub.aclose()
        except Exception:
            logger.warning("worker bus: pub/sub close failed during teardown", exc_info=True)
