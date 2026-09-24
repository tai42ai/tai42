"""Publish an op to the fleet and collect + compute each worker's verdict."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from typing import TYPE_CHECKING, Any

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus.models import (
    _OP_PAYLOAD_KEY,
    _TRANSPORT_ERRORS,
    FleetResult,
    LastOp,
    LocalApplyResult,
    OpOutcome,
    UnknownFleetTargetsError,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
    _beat_age_seconds,
    _decode,
    _merge_terminal,
    presence_fresh,
)
from tai42_skeleton.utils.redis_typing import eval_script

if TYPE_CHECKING:
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


class WorkerBusPublishMixin:
    """The publisher surface of :class:`WorkerBus`: broadcast an op and collect a per-worker outcome.

    Computes verdicts for workers that never reply.
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

        @staticmethod
        def _op_name(op: dict[str, Any]) -> str: ...

        def _local_row(self) -> WorkerRow: ...

    async def publish(
        self,
        op: dict[str, Any],
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
        """
        op_name = self._op_name(op)
        self._validate_local_targets(local, targets)

        if self._local:
            return self._local_publish(op_name, targets, local)

        try:
            fleet = await self._broadcast(op_name, op, targets, expected_at_start)
        except _TRANSPORT_ERRORS as exc:
            logger.error("worker bus: publish of %r failed — bus unreachable", op_name, exc_info=True)
            return FleetResult(op=op_name, reachable=False, error=f"{type(exc).__name__}: {exc}")

        if local is not None:
            fleet.append(self._self_result(local))
        fleet.sort(key=lambda r: r.name)
        return FleetResult(op=op_name, results=fleet)

    async def _broadcast(
        self,
        op_name: str,
        op: dict[str, Any],
        targets: list[str] | None,
        expected_at_start: dict[str, int] | None = None,
    ) -> list[WorkerResult]:
        op_id = uuid.uuid4().hex
        reply_channel = f"{self._settings.reply_prefix}{op_id}"
        identity = self.identity
        async with _pkg.client_ctx(RedisClient, self._settings.redis) as conn:
            r: Any = conn
            expected, gaps = await self._classify_workers(r, targets, expected_at_start)
            # ``r.pubsub()`` is a non-I/O constructor, bound BEFORE the try so pubsub is
            # never unbound in the finally. A raising subscribe still reaches the finally,
            # which unsubscribes then ALWAYS closes the pub/sub (the aclose runs even if
            # the unsubscribe raises) — no leaked connection on a failed join.
            pubsub = r.pubsub()
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
                collected = await self._collect(r, pubsub, expected, gaps, op_id, op_name)
            finally:
                try:
                    await pubsub.unsubscribe(reply_channel)
                finally:
                    await pubsub.aclose()
        return list(collected.values())

    async def _classify_workers(
        self, r: Any, targets: list[str] | None, expected_at_start: dict[str, int] | None = None
    ) -> tuple[dict[str, int | None], dict[str, WorkerResult]]:
        """Split the live fleet into the EXPECTED set and the GAP set.

        EXPECTED is keyed name → generation, awaited for a reply; GAP is keyed name → its
        actual-condition result. A gap row fails the ready+fresh gate, so it is not an expected worker; rather
        than being dropped it is landed as its own condition (``resyncing`` /
        ``recycling`` / ``stale``). Whole-fleet: expected = the READY rows past the
        freshness gate, minus self; every other row is a gap. Targeted: each named
        target minus self — a ready+fresh target is expected, a target present-but-gap
        carries its gap outcome, and a target absent from the census stays expected with
        generation ``None`` so it is reported ``departed`` at the cut.

        This census runs at publish time; ``expected_at_start`` re-admits what the
        caller's earlier op-start census saw — see :meth:`_carry_expected`.
        """
        rows = await self._indexed_workers(r)
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
        else:
            # Targets are NOT re-validated here (the caller validated); an absent target
            # is reported as departed at the cut, a present-but-gap target as its gap
            # outcome.
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
        self._carry_expected(expected, gaps, targets, expected_at_start)
        return expected, gaps

    def _carry_expected(
        self,
        expected: dict[str, int | None],
        gaps: dict[str, WorkerResult],
        targets: list[str] | None,
        expected_at_start: dict[str, int] | None,
    ) -> None:
        """Re-admit a worker censused ready+fresh at op start that this publish-time census no longer sees.

        The window is the caller's own local apply, which runs between the two censuses:
        a worker whose presence TTL faded across it would land in neither set here, and
        an op that never expected it would report converged without it. Re-admitted at
        its SNAPSHOT generation, so the reply gate still discards a replacement life's
        reply and :meth:`_computed_verdict` names the successor when the slot was retaken.

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

    def _gap_result(self, row: WorkerRow) -> WorkerResult:
        """Classify a gap row (one that fails the ready+fresh gate) as its actual condition.

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

    async def _collect(
        self,
        r: Any,
        pubsub: Any,
        expected: dict[str, int | None],
        gaps: dict[str, WorkerResult],
        op_id: str,
        op_name: str,
    ) -> dict[str, WorkerResult]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        ack_deadline = start + self._settings.ack_timeout
        apply_deadline = start + self._settings.apply_timeout
        terminal, acked, transport_error = await self._collect_replies(
            pubsub, expected, op_id, op_name, ack_deadline, apply_deadline
        )
        return await self._finalize_verdicts(r, expected, gaps, terminal, acked, transport_error)

    async def _collect_replies(
        self,
        pubsub: Any,
        expected: dict[str, int | None],
        op_id: str,
        op_name: str,
        ack_deadline: float,
        apply_deadline: float,
    ) -> tuple[dict[str, WorkerResult], set[str], str | None]:
        """Gather terminal/ack replies until the deadline.

        Returns the terminal map, the ack set, and a transport error string if the poll
        hit a blip.
        """
        terminal: dict[str, WorkerResult] = {}
        acked: set[str] = set()
        loop = asyncio.get_running_loop()
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
            phase, name = self._classify_reply_frame(frame, expected, op_id)
            if name is None:
                continue
            if phase == "received":
                acked.add(name)
            elif phase == "terminal":
                _merge_terminal(terminal, name, self._terminal_result(name, frame))
        return terminal, acked, transport_error

    def _classify_reply_frame(
        self, frame: dict[str, Any], expected: dict[str, int | None], op_id: str
    ) -> tuple[str | None, str | None]:
        """Admit/classify a reply frame.

        Returns ``(phase, name)`` for an in-expected frame whose generation and op_id
        match, or ``(None, None)`` when it is discarded (the generation/op_id mismatches
        are logged).
        """
        name = frame.get("name")
        if name not in expected:
            return None, None
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
            return None, None
        if frame.get("op_id") != op_id:
            logger.warning(
                "worker bus: discarding reply from %s — op_id mismatch (expected %s, got %s)",
                name,
                op_id,
                frame.get("op_id"),
            )
            return None, None
        return frame.get("phase"), name

    async def _finalize_verdicts(
        self,
        r: Any,
        expected: dict[str, int | None],
        gaps: dict[str, WorkerResult],
        terminal: dict[str, WorkerResult],
        acked: set[str],
        transport_error: str | None,
    ) -> dict[str, WorkerResult]:
        """Compute the final per-worker verdicts.

        A collected terminal, else a presence-rechecked computed verdict, then the gap
        rows folded in.
        """
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
        ``timed_out`` (acked) or ``missing`` (never acked).
        """
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
        """Re-read a worker's presence key at the report cut.

        Returns ``("absent",
        None)`` when the key is gone, ``("alive", generation)`` when it is present (the
        generation parsed from the value, ``None`` if the value does not parse), or
        ``("unreachable", None)`` when the check itself could not reach Redis (the caller
        degrades to a loud missing/timed_out). The generation lets the verdict see a
        replacement that took the slot mid-apply.
        """
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

    # -- Census + target validation --------------------------------------------

    async def census(self) -> list[WorkerRow]:
        """The live fleet: one :class:`WorkerRow` per registered presence key.

        This is the fleet worker listing (it backs ``GET /api/fleet/workers``). The
        busless variant returns its ONE synthesized ready row, its ``beat_at``
        computed fresh at call time (never frozen at construction).
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

        Applies the SAME ready+fresh gate :meth:`_classify_workers` expects workers by,
        with self excluded (a publisher reports itself from its own local result). Gap
        rows are left out on purpose: they already carry no confirmation promise, so
        carrying one back in would turn a worker that announced its own departure into a
        wait. A busless bus sees only its own row and so snapshots nothing.
        """
        self_name = self.identity.name
        return {
            row.name: row.generation
            for row in await self.census()
            if row.name != self_name and self._is_ready_fresh(row)
        }

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
            rows.append(self._row_from_presence(name, raw, outcomes[2 * i + 1]))
        if stale:
            logger.debug("worker bus: pruning stale presence-index members %s", stale)
            for name in stale:
                await eval_script(r, _PRUNE_STALE_LUA, 2, self._settings.presence_key(name), index, name)
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
