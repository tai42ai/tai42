"""Publish an op to the fleet: census the expected workers, broadcast, and report each worker's outcome."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import TYPE_CHECKING, Any

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus.models import (
    _OP_PAYLOAD_KEY,
    TRANSPORT_ERRORS,
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    UnknownFleetTargetsError,
    WorkerResult,
    WorkerRow,
    WorkerState,
    _beat_age_seconds,
    _PresenceValue,
    presence_fresh,
)
from tai42_skeleton.app.bus.ops import FleetOp, fleet_op_wire
from tai42_skeleton.app.bus.replies import WorkerBusRepliesMixin, _ReplyLink
from tai42_skeleton.utils.redis_typing import eval_script

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tai42_skeleton.app.bus.models import WorkerIdentity
    from tai42_skeleton.app.bus_settings import BusSettings

logger = logging.getLogger(__name__)

# The seam-package object this submodule reads ``client_ctx`` through at call time, so a
# ``monkeypatch.setattr`` on the ``tai42_skeleton.app.bus`` alias bites the pooled
# connection opened here. Captured as this package instance's own object.
_pkg = sys.modules["tai42_skeleton.app.bus"]

# Self-heal a stale index member in ONE server-atomic step: SREM the name ONLY while its
# presence key is absent. Slot names are reused by every re-mint, so between the census's
# GET (which saw the key gone) and this prune a new worker can claim the same name and
# write its key+member; re-checking EXISTS under the SREM keeps that live member in the
# index. KEYS[1] the presence key, KEYS[2] the index, ARGV[1] the member name.
_PRUNE_STALE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return redis.call('SREM', KEYS[2], ARGV[1])
end
return 0
"""


class WorkerBusPublishMixin(WorkerBusRepliesMixin):
    """The publisher surface of :class:`WorkerBus`: broadcast an op and collect a per-worker outcome.

    The replies and the verdicts of workers that never reply are collected by
    :class:`~tai42_skeleton.app.bus.replies.WorkerBusRepliesMixin`.
    """

    if TYPE_CHECKING:
        # State and read surface supplied by the composed WorkerBus (declared here so
        # the mixin type-checks in isolation; the real attributes are set in
        # WorkerBus.__init__ and the methods live on WorkerBus).
        _settings: BusSettings
        _local: bool

        @property
        def identity(self) -> WorkerIdentity:
            """This worker's identity, supplied by the composed :class:`WorkerBus`."""
            ...

        def _local_row(self) -> WorkerRow: ...

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
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
        and here is churn, reported honestly as ``departed`` rather than raised.

        ``expected_at_start`` is the caller's OWN pre-side-effect census (name →
        generation, self excluded), the answer to "who was owed a confirmation when
        this op began". The bus censuses at publish time, which is AFTER a caller's
        local apply; a worker whose presence TTL faded across that apply would be off
        BOTH the expected set and the gap set, and the op would report converged
        without ever having expected it. Every name carried in stays expected at its
        snapshot generation, so it is either collected or given an honest computed
        verdict. See :meth:`_carry_expected` for what it does NOT override.

        An op whose type declares ``awaited_mid_resync`` also awaits each fresh ``resyncing``
        worker (see :meth:`_classify_workers`).
        """
        op_name = op.op
        self._validate_local_targets(local, targets)

        if self._local:
            return self._local_publish(op_name, targets, local)

        try:
            fleet = await self._broadcast(
                op_name,
                fleet_op_wire(op),
                targets,
                expected_at_start,
                await_resyncing=type(op).awaited_mid_resync,
            )
        except TRANSPORT_ERRORS as exc:
            logger.error("worker bus: publish of %r failed — bus unreachable", op_name, exc_info=True)
            return FleetResult(op=op_name, reachable=False, error=f"{type(exc).__name__}: {exc}")

        if local is not None:
            fleet.append(local.as_row(self.identity.name))
        fleet.sort(key=lambda r: r.name)
        return FleetResult(op=op_name, results=fleet)

    async def _broadcast(
        self,
        op_name: str,
        op: dict[str, Any],
        targets: list[str] | None,
        expected_at_start: dict[str, int] | None = None,
        *,
        await_resyncing: bool,
    ) -> list[WorkerResult]:
        op_id = uuid.uuid4().hex
        reply_channel = f"{self._settings.reply_prefix}{op_id}"
        identity = self.identity
        # ``fresh=True``: a dedicated, non-pooled connection, for the reason the subscription
        # states (``_run_subscription`` in ``subscribe.py``): this lease is held across the whole
        # reply collection, up to the apply timeout, and a pooled lease is epoch-stamped, so a
        # reload's retire would wait its drain budget on it and then force-close it, failing the
        # collection and holding the retire. The bus URL never changes across a profile apply.
        async with _pkg.client_ctx(RedisClient, self._settings.redis, fresh=True) as conn:
            r: Any = conn
            expected, gaps, mid_resync = await self._classify_workers(
                r, targets, expected_at_start, await_resyncing=await_resyncing
            )
            # ``r.pubsub()`` is a non-I/O constructor, bound BEFORE the try so pubsub is
            # never unbound in the finally. A raising subscribe still reaches the finally,
            # which unsubscribes then ALWAYS closes the pub/sub (the aclose runs even if
            # the unsubscribe raises) — no leaked connection on a failed join. A reconnect
            # the retry hides inside a read is recorded on ``link`` and reported.
            link = _ReplyLink()
            pubsub = r.pubsub(on_reconnect=link.mark_reconnected)
            try:
                await pubsub.subscribe(reply_channel)
                # The op payload is NESTED under its own reserved envelope key, never
                # flattened alongside the transport fields: an op legitimately carries
                # its own ``name`` (a preset/tool name), and flattening would let the
                # transport identity ``name`` clobber it. Envelope and payload occupy
                # disjoint namespaces so no op field can ever collide with a route field.
                wire = {
                    "name": identity.name,
                    "generation": identity.generation,
                    "op_id": op_id,
                    "reply_to": reply_channel,
                    "targets": targets,
                    _OP_PAYLOAD_KEY: op,
                }
                await r.publish(self._settings.channel, json.dumps(wire))
                collected = await self._collect(r, pubsub, link, expected, gaps, mid_resync, op_id, op_name)
            finally:
                try:
                    await pubsub.unsubscribe(reply_channel)
                finally:
                    await pubsub.aclose()
        return list(collected.values())

    async def _classify_workers(
        self,
        r: Any,
        targets: list[str] | None,
        expected_at_start: dict[str, int] | None = None,
        *,
        await_resyncing: bool = False,
    ) -> tuple[dict[str, int], dict[str, WorkerResult], set[str]]:
        """Split the live fleet into the EXPECTED set and the GAP set.

        EXPECTED is keyed name → generation, awaited for a reply; GAP is keyed name → its
        actual-condition result. An expected row is a ready+fresh row, plus, when
        ``await_resyncing`` (the op declares ``awaited_mid_resync``), a fresh ``resyncing``
        row: that worker subscribed to the op channel before it wrote its row, so the op is
        buffered on its subscription and applied once its resync returns. Every other row is
        a gap; rather than being dropped it is landed as its own condition (``resyncing`` /
        ``recycling`` / ``stale``). Whole-fleet: every row minus self is classified so.
        Targeted: each named target minus self — a present target is classified so, and a
        target absent from the census stays expected: at the generation the caller's op-start
        census saw (:meth:`_carry_expected`), else at the last generation its slot minted
        (:meth:`_last_minted`). Either way it is expected at a known life, so a life read
        during the collection reports it ``departed`` once its slot claim is gone or a newer
        life holds its slot. The third set names the expected workers admitted while resyncing.

        This census runs at publish time; ``expected_at_start`` re-admits what the
        caller's earlier op-start census saw — see :meth:`_carry_expected`.
        """
        rows = await self._indexed_workers(r)
        by_name = {row.name: row for row in rows}
        self_name = self.identity.name
        expected: dict[str, int] = {}
        gaps: dict[str, WorkerResult] = {}
        mid_resync: set[str] = set()
        absent: list[str] = []
        if targets is None:
            for name, row in by_name.items():
                if name != self_name:
                    self._place_row(row, expected, gaps, mid_resync, await_resyncing=await_resyncing)
        else:
            # Targets are NOT re-validated here (the caller validated); an absent target
            # stays expected at a known life, a present-but-gap target lands as its gap outcome.
            for name in targets:
                if name == self_name:
                    continue
                row = by_name.get(name)
                if row is None:
                    absent.append(name)
                else:
                    self._place_row(row, expected, gaps, mid_resync, await_resyncing=await_resyncing)
        self._carry_expected(expected, gaps, targets, expected_at_start)
        expected.update(await self._last_minted(r, [name for name in absent if name not in expected]))
        return expected, gaps, mid_resync

    async def _last_minted(self, r: Any, names: list[str]) -> dict[str, int]:
        """The last generation each slot in ``names`` minted, read in one round trip; ``0`` for a slot never minted.

        A life subscribes to the op channel before it claims its slot and mints its
        generation, so read before the publish, the last minted life is the newest one the
        op can reach: a life that claims the slot later is a newer life, and the collection
        reports the target ``departed`` once it holds the slot. Generations start at 1, so a
        slot never minted is expected at 0 and every life that takes it is a newer one.
        """
        if not names:
            return {}
        raws = await r.mget([self._settings.gen_key(name) for name in names])
        return {name: int(raw) if raw is not None else 0 for name, raw in zip(names, raws, strict=True)}

    def _place_row(
        self,
        row: WorkerRow,
        expected: dict[str, int],
        gaps: dict[str, WorkerResult],
        mid_resync: set[str],
        *,
        await_resyncing: bool,
    ) -> None:
        """Land one present row as expected (named in ``mid_resync`` when admitted resyncing) or as its gap."""
        if self._is_expected(row, await_resyncing=await_resyncing):
            expected[row.name] = row.generation
            if row.state == WorkerState.resyncing:
                mid_resync.add(row.name)
        else:
            gaps[row.name] = self._gap_result(row)

    def _carry_expected(
        self,
        expected: dict[str, int],
        gaps: dict[str, WorkerResult],
        targets: list[str] | None,
        expected_at_start: dict[str, int] | None,
    ) -> None:
        """Re-admit a worker censused ready+fresh at op start that this publish-time census no longer sees.

        The window is the caller's own local apply, which runs between the two censuses:
        a worker whose presence TTL faded across it would land in neither set here, and
        an op that never expected it would report converged without it. Re-admitted at
        its SNAPSHOT generation, so the reply gate still discards a replacement life's
        reply and the ``departed`` verdict names the successor when the slot was retaken.

        Deliberately narrow. It never overrides a live classification — a carried name
        the census still sees keeps whatever this census decided, including a gap outcome
        (a worker that announced ``recycling`` is departing on purpose and is reported as
        such, not awaited). It never widens ``targets``, and never re-admits self (the
        publisher reports itself from its own local result). Each re-admission is logged:
        a live worker going off-census mid-op is an anomaly even when it later confirms.
        """
        if not expected_at_start:
            return
        self_name = self.identity.name
        allowed = None if targets is None else set(targets)
        for name, generation in expected_at_start.items():
            if name == self_name or name in expected or name in gaps:
                continue
            if allowed is not None and name not in allowed:
                continue
            logger.warning(
                "worker bus: %s was ready at op start but is off the census at publish — "
                "still expecting its confirmation at generation %s",
                name,
                generation,
            )
            expected[name] = generation

    def _is_ready_fresh(self, row: WorkerRow) -> bool:
        """The ready+fresh gate: a row is expected only when it advertises ``ready`` and its PTTL clears the bound."""
        return row.state == WorkerState.ready and presence_fresh(row.pttl_ms, self._settings.heartbeat_ttl)

    def _is_expected(self, row: WorkerRow, *, await_resyncing: bool) -> bool:
        """Whether a census row owes a reply: ready+fresh, or fresh ``resyncing`` when ``await_resyncing``.

        A ``recycling`` row is departing, and a decayed row (a worker that died mid-resync
        among them) carries no promise, so both stay gaps.
        """
        if self._is_ready_fresh(row):
            return True
        return (
            await_resyncing
            and row.state == WorkerState.resyncing
            and presence_fresh(row.pttl_ms, self._settings.heartbeat_ttl)
        )

    def _gap_result(self, row: WorkerRow) -> WorkerResult:
        """Classify a gap row (one :meth:`_is_expected` does not admit) as its actual condition.

        A decayed row is ``stale`` regardless of its written state — a worker that died
        mid-resync/mid-recycle carries no convergence promise; a fresh resyncing/recycling
        row keeps its written state as the outcome.
        """
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

    def _local_publish(self, op_name: str, targets: list[str] | None, local: LocalApplyResult | None) -> FleetResult:
        if targets is not None:
            unknown = sorted(set(targets) - {self.identity.name})
            if unknown:
                raise UnknownFleetTargetsError(
                    f"worker bus (local): cannot reach targets not on this process: {unknown}"
                )
        results = [local.as_row(self.identity.name)] if local is not None else []
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

    # -- Census + target validation --------------------------------------------

    async def census(self) -> list[WorkerRow]:
        """The live fleet: one :class:`WorkerRow` per registered presence key.

        This is the fleet worker listing (it backs ``GET /api/fleet/workers``). The
        busless variant returns its ONE synthesized ready row, its ``beat_at``
        computed fresh at call time (never frozen at construction). On an outage it
        raises one of :data:`TRANSPORT_ERRORS` — a read that could not happen is never
        a silent empty fleet.
        """
        if self._local:
            return [self._local_row()]
        async with _pkg.client_ctx(RedisClient, self._settings.redis) as conn:
            return await self._indexed_workers(conn)

    async def expected_at_start(self) -> dict[str, int]:
        """The siblings owed a confirmation right now, as ``{name: generation}``.

        The snapshot a publisher takes BEFORE its own local side effect and hands back to
        :meth:`publish` as ``expected_at_start``.

        :meth:`publish` censuses when it is called, which for a publisher that applies
        locally first is after that apply; a worker whose presence TTL fades across the
        apply drops off that census entirely and the op reports converged without it.
        Reading membership here, before the side effect, makes the apply part of the
        op's window rather than a blind spot.

        Applies the ready+fresh gate :meth:`_classify_workers` expects every op's workers by,
        with self excluded (a publisher reports itself from its own local result). Gap
        rows are left out on purpose: they already carry no confirmation promise, so
        carrying one back in would turn a worker that announced its own departure into a
        wait. A busless bus sees only its own row and so snapshots nothing.
        """
        return self.expected_from_census(await self.census())

    def expected_from_census(self, rows: Sequence[WorkerRow]) -> dict[str, int]:
        """The siblings owed a confirmation by census ``rows`` the caller already read, as ``{name: generation}``.

        The snapshot :meth:`expected_at_start` takes, for a publisher that reads the census once
        before its local side effect and derives more than the snapshot from it.
        """
        self_name = self.identity.name
        return {row.name: row.generation for row in rows if row.name != self_name and self._is_ready_fresh(row)}

    async def validate_targets(self, targets: list[str] | None) -> None:
        """Raise naming any target absent from the census.

        A caller-side seam run BEFORE the caller's local apply, so validation precedes side
        effects.

        ``targets=None`` (whole fleet) is always valid. A typo'd worker name is an
        error here, never a silent narrowing at publish time.
        """
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

    async def _indexed_workers(self, r: Any) -> list[WorkerRow]:
        index = self._settings.presence_index
        members = await r.smembers(index)
        names = sorted(m.decode() if isinstance(m, bytes) else m for m in members)
        if not names:
            return []
        # One round-trip for every index member: each member's presence-key GET and PTTL
        # are queued ADJACENT in a single pipeline, so the remaining PTTL is captured
        # ALONGSIDE the value (the freshness gate stays clock-independent, never a
        # worker-stamped beat_at against our clock) without a sequential read per worker.
        keys = [self._settings.presence_key(name) for name in names]
        pipe = r.pipeline(transaction=False)
        for key in keys:
            pipe.get(key)
            pipe.pttl(key)
        outcomes = await pipe.execute()
        rows: list[WorkerRow] = []
        stale: list[str] = []
        for i, name in enumerate(names):
            raw = outcomes[2 * i]
            if raw is None:
                # In the index but its presence key is gone (TTL expired) — not live: drop
                # it from this census and prune the index member so the set self-heals on
                # read. The prune re-checks the key server-side, so a name re-minted live
                # between the GET and here is not removed.
                stale.append(name)
                continue
            pttl_ms = outcomes[2 * i + 1]
            rows.append(
                _PresenceValue.model_validate_json(raw).row(name, int(pttl_ms) if pttl_ms is not None else None)
            )
        if stale:
            logger.debug("worker bus: pruning stale presence-index members %s", stale)
            for name in stale:
                await eval_script(r, _PRUNE_STALE_LUA, 2, self._settings.presence_key(name), index, name)
        return rows
