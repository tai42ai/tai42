"""Collect a published op's replies and compute the verdict of every expected worker that sends none."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from tai42_skeleton.app.bus.models import (
    TRANSPORT_ERRORS,
    OpOutcome,
    WorkerResult,
    WorkerState,
    _decode,
    _merge_terminal,
    _PresenceValue,
    heartbeat_interval,
)

if TYPE_CHECKING:
    from tai42_skeleton.app.bus_settings import BusSettings

logger = logging.getLogger(__name__)

_RECONNECTED_CAUSE = (
    "the reply subscription reconnected during reply collection; a reply sent while it was disconnected is lost"
)

# The detail a report-cut verdict carries when the re-read of the worker's life could not reach Redis.
_UNREAD_AT_CUT = "its life could not be re-read at the report cut: the bus read failed"

# What one poll of the reply subscription got: a message, nothing within its timeout, or a
# transport error that ends the collection.
_Poll = Literal["message", "quiet", "broken"]


@dataclass(frozen=True)
class _Life:
    """What the bus shows of one slot's life: whether its claim is held, and the last generation it minted.

    A life holds its slot claim from its claim to its end, renewing it on every heartbeat, and
    a claim once lost is never renewed again: a life whose claim is gone has ended. Every claim
    mints the slot's next generation, so a generation greater than the one a worker was
    expected at means a newer life took the slot. ``generation`` is ``0`` for a slot never minted.
    """

    claimed: bool
    generation: int


def _queue_life_read(pipe: Any, settings: BusSettings, name: str) -> None:
    """Queue the two reads of the slot ``name``'s life on ``pipe``: its claim, then its generation counter."""
    pipe.exists(settings.slot_key(name))
    pipe.get(settings.gen_key(name))


def _life_of(claim_count: Any, generation_raw: Any) -> _Life:
    """The :class:`_Life` the two reads :func:`_queue_life_read` queued returned."""
    return _Life(claimed=bool(claim_count), generation=int(generation_raw) if generation_raw is not None else 0)


class _ReplyLink:
    """Whether the reply pub/sub reconnected; its reconnect hook sets ``reconnected``."""

    def __init__(self) -> None:
        """Start with no reconnect seen."""
        self.reconnected = False

    def mark_reconnected(self) -> None:
        """Record a reconnect of the reply pub/sub."""
        self.reconnected = True


@dataclass
class _Collection:
    """What one op's reply collection gathered.

    ``terminal`` holds the wire ``applied``/``failed`` replies, ``acked`` the names that sent
    the ``received`` ack, ``gone`` the ``departed`` verdict of each worker whose life the bus
    showed ended while the publisher waited, and ``transport_error`` the cause of a reply
    that may have been lost.
    """

    terminal: dict[str, WorkerResult] = field(default_factory=dict)
    acked: set[str] = field(default_factory=set)
    gone: dict[str, WorkerResult] = field(default_factory=dict)
    transport_error: str | None = None

    def outstanding(self, expected: dict[str, int]) -> set[str]:
        """The expected names still awaited: neither a terminal reply nor a gone life."""
        return expected.keys() - self.terminal.keys() - self.gone.keys()


class WorkerBusRepliesMixin:
    """Reply collection for :class:`WorkerBus`: gather each expected worker's replies, then its verdict."""

    if TYPE_CHECKING:
        # Supplied by the composed WorkerBus (set in WorkerBus.__init__).
        _settings: BusSettings

    async def _collect(
        self,
        r: Any,
        pubsub: Any,
        link: _ReplyLink,
        expected: dict[str, int],
        gaps: dict[str, WorkerResult],
        mid_resync: set[str],
        op_id: str,
        op_name: str,
    ) -> dict[str, WorkerResult]:
        apply_deadline = asyncio.get_running_loop().time() + self._settings.apply_timeout
        collection = await self._collect_replies(r, pubsub, link, expected, op_id, op_name, apply_deadline)
        return await self._finalize_verdicts(r, expected, gaps, mid_resync, collection)

    async def _collect_replies(
        self,
        r: Any,
        pubsub: Any,
        link: _ReplyLink,
        expected: dict[str, int],
        op_id: str,
        op_name: str,
        apply_deadline: float,
    ) -> _Collection:
        """Gather terminal/ack replies until every expected worker is answered or gone, or the apply deadline.

        Once per heartbeat interval the outstanding workers' lives are read (see
        :meth:`_read_gone_lives`): a worker whose life ended is reported ``departed`` then and
        no longer awaited. A live worker renews its slot claim from its heartbeat while it
        applies (a thread of its own, or under a process prelude a task of its loop that a
        synchronous step of the apply delays), so a slow apply is awaited to the deadline.
        A ``received`` ack decides nothing here: at the report cut it tells an acked worker
        (``timed_out``) from a silent one (``missing``). Every reply already delivered to
        the reply subscription is folded before the collection ends with a gone life, so a
        worker that replied and then left is reported by its reply.

        A transport error on the reply poll ends the collection; a reconnect of the reply
        subscription that the retry hid inside a read does not, as the replies sent after it
        still arrive. Either is recorded on the collection, logged, and named on every worker
        left without a terminal reply, since a reply sent across it is lost.
        """
        collection = _Collection()
        loop = asyncio.get_running_loop()
        interval = heartbeat_interval(self._settings.heartbeat_ttl)
        next_life_read = loop.time() + interval

        while collection.outstanding(expected):
            now = loop.time()
            if now >= apply_deadline:
                break
            if now >= next_life_read:
                await self._read_gone_lives(r, expected, op_name, collection)
                next_life_read = loop.time() + interval
                continue
            timeout = max(0.01, min(apply_deadline, next_life_read) - now)
            if await self._receive(pubsub, link, expected, op_id, op_name, collection, timeout) == "broken":
                return collection
        if collection.gone:
            await self._fold_delivered(pubsub, link, expected, op_id, op_name, collection)
        return collection

    async def _receive(
        self,
        pubsub: Any,
        link: _ReplyLink,
        expected: dict[str, int],
        op_id: str,
        op_name: str,
        collection: _Collection,
        timeout: float,
    ) -> _Poll:
        """Poll the reply subscription once, up to ``timeout``, and fold what it delivered."""
        try:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        except TRANSPORT_ERRORS as exc:
            # A blip mid-collection is reported loudly on the affected workers, never
            # silently dropped.
            collection.transport_error = f"{type(exc).__name__}: {exc}"
            logger.error("worker bus: reply collection for %r hit a transport error", op_name, exc_info=True)
            return "broken"
        if link.reconnected and collection.transport_error is None:
            collection.transport_error = _RECONNECTED_CAUSE
            logger.warning("worker bus: reply collection for %r: %s", op_name, _RECONNECTED_CAUSE)
        if msg is None:
            return "quiet"
        self._fold_reply(msg, expected, op_id, collection)
        return "message"

    async def _fold_delivered(
        self,
        pubsub: Any,
        link: _ReplyLink,
        expected: dict[str, int],
        op_id: str,
        op_name: str,
        collection: _Collection,
    ) -> None:
        """Fold every reply already delivered to the reply subscription, without waiting for more.

        A terminal reply folded here outranks a gone verdict (see :meth:`_finalize_verdicts`).
        """
        while await self._receive(pubsub, link, expected, op_id, op_name, collection, 0) == "message":
            pass

    async def _read_gone_lives(self, r: Any, expected: dict[str, int], op_name: str, collection: _Collection) -> None:
        """Record ``departed`` for every outstanding worker whose life the bus shows ended.

        One pipelined read of the outstanding workers' slot claims and generation counters
        (:class:`_Life`). A life is ended when its slot claim is gone (a deliberate stop releases
        it, a lapsed claim's TTL removes it, and a life whose claim is gone never renews it) or
        when its slot minted a GREATER generation than the one expected (a newer life took the
        slot). A presence row that lapsed while the claim holds ends nothing: the life
        re-establishes its row on its next connect. A read that cannot reach Redis proves
        nothing: it is logged and every worker stays awaited.
        """
        names = sorted(collection.outstanding(expected))
        try:
            pipe = r.pipeline(transaction=False)
            for name in names:
                _queue_life_read(pipe, self._settings, name)
            reads = await pipe.execute()
        except TRANSPORT_ERRORS:
            logger.error(
                "worker bus: life read during reply collection for %r failed; still awaiting %s",
                op_name,
                names,
                exc_info=True,
            )
            return
        for i, name in enumerate(names):
            life = _life_of(reads[2 * i], reads[2 * i + 1])
            verdict = self._gone_verdict(name, expected[name], life, acked=name in collection.acked)
            if verdict is not None:
                collection.gone[name] = verdict

    def _fold_reply(
        self,
        msg: dict[str, Any],
        expected: dict[str, int],
        op_id: str,
        collection: _Collection,
    ) -> None:
        """Fold one reply message into the collection; an undecodable or discarded frame is skipped."""
        frame = _decode(msg["data"])
        if frame is None:
            return
        phase, name = self._classify_reply_frame(frame, expected, op_id)
        if name is None:
            return
        if phase == "received":
            collection.acked.add(name)
        elif phase == "terminal":
            _merge_terminal(collection.terminal, name, self._terminal_result(name, frame))

    def _classify_reply_frame(
        self, frame: dict[str, Any], expected: dict[str, int], op_id: str
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
        if got_gen != expected_gen:
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
        expected: dict[str, int],
        gaps: dict[str, WorkerResult],
        mid_resync: set[str],
        collection: _Collection,
    ) -> dict[str, WorkerResult]:
        """Compute the final per-worker verdicts.

        A collected terminal reply, else the ``departed`` verdict of a life seen gone during
        the collection, else a computed verdict on the life re-read at the report cut; then the
        gap rows folded in.
        """
        results: dict[str, WorkerResult] = {}
        for name in expected:
            verdict = collection.terminal.get(name)
            if verdict is None:
                verdict = collection.gone.get(name)
            if verdict is None:
                verdict = await self._computed_verdict(
                    r,
                    name,
                    expected[name],
                    name in collection.acked,
                    collection.transport_error,
                    admitted_mid_resync=name in mid_resync,
                )
            results[name] = verdict
        # Gap rows were never expected to reply; land
        # each as its own actual condition (resyncing/recycling/stale) rather than
        # dropping it. Disjoint from ``expected`` by construction.
        results.update(gaps)
        return results

    def _terminal_result(self, name: str, frame: dict[str, Any]) -> WorkerResult:
        raw = frame.get("outcome")
        outcome = OpOutcome.failed if raw == OpOutcome.failed.value else OpOutcome.applied
        return WorkerResult(name=name, outcome=outcome, payload=frame.get("payload"), error=frame.get("error"))

    def _gone_verdict(self, name: str, expected_gen: int, life: _Life, *, acked: bool) -> WorkerResult | None:
        """The ``departed`` verdict of a worker whose expected life ``life`` shows ended, else ``None``.

        Generation-aware: a slot that minted a generation GREATER than the one expected at
        publish means a newer life took it (the reply gate discards the new life's replies). A
        slot whose claim is gone means the life ended.
        """
        if life.generation > expected_gen:
            return WorkerResult(
                name=name,
                outcome=OpOutcome.departed,
                detail=f"replaced by generation {life.generation} mid-apply",
            )
        if life.claimed:
            return None
        if acked:
            return WorkerResult(
                name=name,
                outcome=OpOutcome.departed,
                detail="acked, then its slot claim ended before it applied — worker departed mid-apply",
            )
        return WorkerResult(
            name=name,
            outcome=OpOutcome.departed,
            detail="no reply and its slot holds no claim — worker departed",
        )

    async def _computed_verdict(
        self,
        r: Any,
        name: str,
        expected_gen: int,
        acked: bool,
        transport_error: str | None,
        *,
        admitted_mid_resync: bool = False,
    ) -> WorkerResult:
        """Classify a worker that holds no terminal reply, re-reading its life at the report cut.

        A life the re-read shows ended is ``departed`` (:meth:`_gone_verdict`). A life that still
        holds its slot claim and whose presence reads ``recycling`` began leaving after publish:
        its stop ends the op it was applying, so it is ``recycling``, converging by ending this
        life. Any other slot claim the expected life still holds is ``timed_out`` (acked) or
        ``missing`` (never acked). A never-acked worker admitted mid-resync whose presence still
        reads ``resyncing`` has not read the op yet, and its ``missing`` detail says so. A
        re-read that cannot reach Redis leaves the life unknown: the worker is ``timed_out`` or
        ``missing`` as it acked, and the detail says the life was not re-read.
        """
        cut = await self._reread_life(r, name)
        state: WorkerState | None = None
        if cut is not None:
            life, state = cut
            gone = self._gone_verdict(name, expected_gen, life, acked=acked)
            if gone is not None:
                return gone
        outcome = OpOutcome.timed_out if acked else OpOutcome.missing
        if state == WorkerState.recycling:
            outcome = OpOutcome.recycling
            leaving = "began leaving before it replied — it converges by ending this life, not by applying this op"
            detail = f"acked, then {leaving}" if acked else leaving
        elif acked:
            detail = (
                "acked but did not apply within the apply timeout, which covers the worker's full drain budget — "
                "its reload is stuck past it; read the worker's log, verify via fleet census/reload"
            )
        elif cut is None:
            detail = "no received-ack within the apply timeout — worker missing"
        elif admitted_mid_resync and state == WorkerState.resyncing:
            detail = (
                "no received-ack: the worker was resyncing at publish and applies this op after its resync, "
                "which outlasted the apply timeout"
            )
        else:
            detail = (
                "no received-ack within the apply timeout while its slot claim stayed held — "
                "worker missing (alive but silent)"
            )
        if cut is None:
            detail = f"{detail} ({_UNREAD_AT_CUT})"
        if transport_error is not None:
            detail = f"{detail} (reply collection transport error: {transport_error})"
        return WorkerResult(name=name, outcome=outcome, detail=detail)

    async def _reread_life(self, r: Any, name: str) -> tuple[_Life, WorkerState | None] | None:
        """Re-read a worker's life and the state its presence row reads at the report cut, in one round trip.

        Returns the :class:`_Life` and the row's state (``None`` when the row is absent), or
        ``None`` when the read could not reach Redis (logged at ERROR). The life lets the
        verdict see an ended life or a newer one in the slot; the state, a worker that began
        leaving (``recycling``) or one that is still resyncing. A row that is not a presence value raises.
        """
        try:
            pipe = r.pipeline(transaction=False)
            _queue_life_read(pipe, self._settings, name)
            pipe.get(self._settings.presence_key(name))
            claim_count, generation_raw, raw = await pipe.execute()
        except TRANSPORT_ERRORS:
            logger.error("worker bus: life re-read at the report cut for %s failed", name, exc_info=True)
            return None
        state = _PresenceValue.model_validate_json(raw).state if raw is not None else None
        return _life_of(claim_count, generation_raw), state
