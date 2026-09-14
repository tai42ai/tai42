"""Subscribe to the fleet channel, hold the slot claim + heartbeat, and apply
delivered ops."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus.models import (
    _OP_PAYLOAD_KEY,
    _TRANSPORT_ERRORS,
    LastOp,
    OpOutcome,
    SlotLostError,
    WorkerIdentity,
    WorkerState,
    _decode,
    _PresenceValue,
    _utcnow_iso,
)
from tai42_skeleton.utils.redis_typing import eval_script

if TYPE_CHECKING:
    from tai42_skeleton.app.bus.models import WorkerKind
    from tai42_skeleton.app.bus_settings import BusSettings

logger = logging.getLogger(__name__)

# The seam-package object this submodule reads ``client_ctx`` through at call time, so a
# ``monkeypatch.setattr`` on the ``tai42_skeleton.app.bus`` alias bites the pooled
# connection opened here. Captured as this package instance's own object.
_pkg = sys.modules["tai42_skeleton.app.bus"]

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


class WorkerBusSubscribeMixin:
    """The subscriber surface of :class:`WorkerBus`: claim a slot, hold its presence
    heartbeat, and apply delivered ops with a two-phase confirmation."""

    if TYPE_CHECKING:
        # State and read surface supplied by the composed WorkerBus (declared here so
        # the mixin type-checks in isolation; the real attributes are set in
        # WorkerBus.__init__ and the ``identity`` property lives on WorkerBus).
        _settings: BusSettings
        _claim_token: str
        _local: bool
        _identity: WorkerIdentity | None
        _joined_at: str | None
        _presence: _PresenceValue | None
        _kind: WorkerKind
        _pid: int
        _post_reply_slot: Callable[[], None] | None
        _backoff_initial: float
        _backoff_max: float
        _backoff_factor: float

        @property
        def identity(self) -> WorkerIdentity: ...

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
        async with _pkg.client_ctx(RedisClient, self._settings.redis, fresh=True) as conn:
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
        admitted = self._admit_op_frame(msg)
        if admitted is None:
            return
        op_payload, responder, reply_to, _op_id = admitted
        await self._apply_op_and_reply(r, callback, op_payload, responder, reply_to)

    def _admit_op_frame(self, msg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None, Any] | None:
        """Admit an op frame: decode, echo-skip by ``(name, generation)``, filter by
        targets, validate the object payload. Returns ``(op_payload, responder,
        reply_to, op_id)`` for a frame to apply, or ``None`` when it is not ours."""
        frame = _decode(msg["data"])
        if frame is None:
            return None
        identity = self.identity
        if frame.get("name") == identity.name and frame.get("generation") == identity.generation:
            # Echo-skip by (name, generation): the publisher applied this op locally
            # before broadcasting and synthesizes its own self entry — delivering it
            # back would double-apply. A frame from an earlier life under the same name
            # (a different generation) is NOT ours and is applied normally.
            return None
        targets = frame.get("targets")
        # Test presence, not truthiness: ``targets is None`` reaches every worker, a
        # list reaches exactly its members — so an empty list reaches nobody, matching
        # the publisher's empty expected set (no silent sibling apply).
        if targets is not None and identity.name not in targets:
            return None
        # The op payload rides nested under its reserved envelope key; a frame that is
        # missing it (or carries a non-object there) is malformed and discarded rather
        # than half-applied.
        op_payload = frame.get(_OP_PAYLOAD_KEY)
        if not isinstance(op_payload, dict):
            logger.warning("worker bus: discarding op frame with no object payload: %r", frame)
            return None
        reply_to = frame.get("reply_to")
        op_id = frame.get("op_id")
        responder = {"name": identity.name, "generation": identity.generation, "op_id": op_id}
        return op_payload, responder, reply_to, op_id

    async def _apply_op_and_reply(
        self,
        r: Any,
        callback: Callable[[dict[str, Any]], Awaitable[Any]],
        op_payload: dict[str, Any],
        responder: dict[str, Any],
        reply_to: str | None,
    ) -> None:
        """Run the callback, build and send the terminal (applied/failed) frame after
        the received ack, stamp last-op, and fire the armed post-reply slot on a clean
        apply. The slot is disarmed on every exit."""
        identity = self.identity
        if reply_to:
            await self._reply(r, reply_to, {**responder, "phase": "received"})
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
