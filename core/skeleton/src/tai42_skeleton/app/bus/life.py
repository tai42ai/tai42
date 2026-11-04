"""One process life on the census: its slot claim, its presence heartbeat and its presence row.

A life begins with the claim (``{kind}-{n}``, won by ``SET NX`` and a fresh generation) the
subscription makes on its first connect, and ends with :meth:`WorkerBusLifeMixin.leave_census`,
which the process's stop runs once its shutdown handlers are over and before its resources are
torn down: the heartbeat is joined, the claim released and the row deleted. Between the two the
heartbeat renews the claim and rewrites the row — on a thread of its own, or, in a process run under a
process prelude, as a task of the subscription's loop (:class:`_Heartbeat`) — while the subscription is
connected and through the whole stop. While the subscription runs it supervises the heartbeat: one
that ends (a transport error, a lost slot) takes the subscription down with it, and the reconnect
starts a new one. Nothing beats while the reconnect backs off: the row lives on its TTL meanwhile,
and a backoff that outlasts the TTL leaves the life off the census until the reconnect registers
its row again, even once the bus is reachable again. Once the subscription ended on purpose, at
the stop, nothing restarts the heartbeat: one that ends then is logged at ERROR, with its cause,
the moment it ends, and the row lapses at its TTL while the process still runs. The row reads
``recycling``, with the drain the process announced, from the moment the life begins leaving —
its departure decision or the first stop signal (:meth:`mark_recycling`), kept across a
reconnect — until the life ends.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from tai42_contract.backend import PROCESS_PRELUDE_ENV
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus.models import (
    TRANSPORT_ERRORS,
    SlotLostError,
    WorkerIdentity,
    WorkerState,
    _PresenceValue,
    _utcnow_iso,
    heartbeat_interval,
)
from tai42_skeleton.config.deployment_env import published_deployment_keys, published_env_readers
from tai42_skeleton.utils.redis_typing import eval_script

if TYPE_CHECKING:
    from tai42_skeleton.app.bus.exit_latch import ExitLatch
    from tai42_skeleton.app.bus.models import WorkerKind
    from tai42_skeleton.app.bus_settings import BusSettings

logger = logging.getLogger(__name__)

# The seam-package object this submodule reads ``client_ctx`` through at call time, so a
# ``monkeypatch.setattr`` on the ``tai42_skeleton.app.bus`` alias bites the connections
# opened here. Captured as this package instance's own object.
_pkg = sys.modules["tai42_skeleton.app.bus"]

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


class _Heartbeat:
    """One life's presence heartbeat: a daemon thread with an event loop of its own, or a task of the starting loop.

    Where the process's threads are OS threads, the heartbeat runs on a daemon thread of
    its own: the subscription's loop can be held synchronously for longer than the claim
    TTL (a config reload's rebuild runs on it), so the beat never shares it. In a process
    run under a process prelude (``TAI_PROCESS_PRELUDE``, set for a green pool) every
    thread is a green thread of the one OS thread that already runs the subscription's
    loop, where a second event loop cannot run, so the heartbeat is a task of that loop
    instead, and it beats only while that loop runs: a hold of the loop longer than a
    third of the TTL (a re-importing rebuild) can leave the row stale to the census, and
    a hold longer than the TTL lets it lapse while the process still runs. Only the home
    differs: both run the same body, and its outcome lands on
    :attr:`done` (an exception, or ``None`` for a clean return or a requested stop),
    which the subscription supervises while it runs, a watch logs once the subscription
    ended (:meth:`watch_unsupervised`), and the stop of the heartbeat awaits from its own
    loop. Its outcome is logged once, by whichever of them takes it first
    (:meth:`take_surfacing`). ``request_stop`` cancels the body on its loop and is safe
    before the body has started and after it has finished.
    """

    def __init__(self, name: str, main: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Prepare (not start) the heartbeat named ``name`` that will run ``main``."""
        self.name = name
        self._main = main
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        # Set by the one that logs the body's outcome, so no other logs it a second time.
        self._surfaced = False
        self.done: Future[None] = Future()
        # The home the heartbeat runs in, set by ``start``: a thread, or a task of the starting loop.
        self.thread: threading.Thread | None = None
        self._home_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the heartbeat: a task of the running loop under a process prelude, a thread of its own otherwise."""
        # RUNNING from the first instant, so a cancel of a future wrapped around
        # ``done`` on the subscription's loop can never cancel the bridge itself.
        self.done.set_running_or_notify_cancel()
        if os.environ.get(PROCESS_PRELUDE_ENV):
            # An empty context, as a thread of its own starts with: the beat carries none of the starter's.
            self._home_task = asyncio.get_running_loop().create_task(
                self._run_as_task(), name=self.name, context=contextvars.Context()
            )
            return
        self.thread = threading.Thread(target=self._run_on_thread, name=self.name, daemon=True)
        self.thread.start()

    def join(self, timeout: float) -> None:
        """Wait up to ``timeout`` for a thread home to finish; a task home has nothing left to join."""
        if self.thread is not None:
            self.thread.join(timeout)

    def _run_on_thread(self) -> None:
        try:
            asyncio.run(self._body())
        except BaseException as exc:
            # Nothing above this frame sees a thread's exception, so it is carried
            # across the bridge to the bus that supervises it.
            self._land(exc)
        else:
            self._land(None)

    async def _run_as_task(self) -> None:
        try:
            await self._body()
        except BaseException as exc:
            # Carried to the bus that supervises it, as from a thread home.
            self._land(exc)
        else:
            self._land(None)

    def _land(self, exc: BaseException | None) -> None:
        """Resolve :attr:`done` with the body's outcome."""
        if exc is None or isinstance(exc, asyncio.CancelledError):
            # Only ``request_stop`` cancels the body: the stop the bus asked for.
            self.done.set_result(None)
        else:
            self.done.set_exception(exc)

    async def _body(self) -> None:
        with self._lock:
            if self._stop_requested:
                return
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        try:
            await self._main()
        finally:
            # Unpublish the loop before a thread home's ``asyncio.run`` closes it, so a
            # late ``request_stop`` never schedules onto a closed loop.
            with self._lock:
                self._loop = None
                self._task = None

    def take_surfacing(self) -> bool:
        """Claim the logging of the body's outcome: ``True`` for the first caller only."""
        with self._lock:
            if self._surfaced:
                return False
            self._surfaced = True
            return True

    def watch_unsupervised(self, life: str) -> None:
        """Log this heartbeat's end at ERROR, with its cause, the moment it ends on its own.

        Armed when the subscription that supervised it ended on purpose: the heartbeat then keeps
        the life's claim and row fresh through the process's stop, and nothing recovers it, so a
        heartbeat that ends (a transport error, a lost slot) leaves the row to expire at its TTL
        while the process still runs. A requested stop logs nothing; a heartbeat that already
        ended is logged at once. The callback runs on the thread that resolves :attr:`done`.
        """

        def _log_end(done: Future[None]) -> None:
            with self._lock:
                requested = self._stop_requested
            if requested:
                return
            exc = done.exception()
            if exc is None:
                exc = RuntimeError("worker bus: presence heartbeat returned without an error")
            if self.take_surfacing():
                logger.error(
                    "worker bus: presence heartbeat for %s ended during the process's stop, with no subscription "
                    "to recover it — the census row lapses at its TTL while this process still runs",
                    life,
                    exc_info=exc,
                )

        self.done.add_done_callback(_log_end)

    def request_stop(self) -> None:
        """Ask the body to stop: cancel it on its loop, or keep it from starting."""
        with self._lock:
            self._stop_requested = True
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)


class WorkerBusLifeMixin:
    """The census-life surface of :class:`WorkerBus`: claim a slot, keep its row fresh, end the life.

    The subscription (:class:`~tai42_skeleton.app.bus.subscribe.WorkerBusSubscribeMixin`) claims
    the slot and starts the heartbeat on its first connect; the life outlives the subscription's
    deliberate end and is ended by :meth:`leave_census`.
    """

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
        _exit_drain: Callable[[], float | None]
        _exit_latch: ExitLatch
        _heartbeat: _Heartbeat | None

        @property
        def identity(self) -> WorkerIdentity:
            """This worker's identity, supplied by the composed :class:`WorkerBus`."""
            ...

    async def _establish_identity(self, r: Any) -> None:
        """(Re)establish this process's claimed slot identity on a (re)connect.

        A held claim that still renews keeps its name+generation+joined_at across the
        reconnect (scoped to a never-lost claim). A lost or absent claim
        (renew miss on the old name, or no prior identity on first boot) mints a NEW
        life: the lowest free ``{kind}-{n}`` won by ``SET NX`` + a fresh ``INCR``.
        """
        if self._identity is not None and await self._renew_claim(r, self._identity.name):
            return
        self._identity = await self._claim_slot(r)
        self._joined_at = _utcnow_iso()

    async def _claim_slot(self, r: Any) -> WorkerIdentity:
        """Win the lowest free ``{kind}-{n}`` slot and mint its generation.

        ``SET <token> NX PX`` on the claim key claims the name atomically; the first
        free ordinal wins. ``INCR`` on the per-name generation counter mints a
        monotonic life number — never re-INCRed by a worker that cannot prove the
        claim (this runs ONLY on a fresh successful claim).
        """
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
        """Extend the claim TTL iff this process still holds the token (one atomic compare-token Lua step).

        Returns False on a miss (token mismatch or absent key) — a lost slot.
        """
        px = int(self._settings.heartbeat_ttl * 1000)
        result = await eval_script(r, _RENEW_LUA, 1, self._settings.slot_key(name), self._claim_token, px)
        return bool(result)

    async def _release_claim(self, r: Any, name: str) -> bool:
        """Delete the claim key iff this process still holds the token (one atomic compare-token Lua step).

        Returns False on a miss — the slot already belongs to a new holder, so nothing was
        released.
        """
        result = await eval_script(r, _RELEASE_LUA, 1, self._settings.slot_key(name), self._claim_token)
        return bool(result)

    def _start_heartbeat(self, name: str, presence_key: str) -> _Heartbeat:
        """Start this life's heartbeat and record it as the bus's one live heartbeat."""
        heartbeat = _Heartbeat(f"tai-worker-bus-heartbeat-{name}", lambda: self._heartbeat_main(presence_key))
        self._heartbeat = heartbeat
        heartbeat.start()
        return heartbeat

    async def _heartbeat_main(self, presence_key: str) -> None:
        """The heartbeat's body: the beat loop over a connection of its own.

        redis-py's asyncio connections belong to the loop that opened them, so a
        thread home opens its own; ``fresh=True`` keeps it outside the epoch client pool,
        so an epoch retire's drain never closes it (the subscription's own connection
        is fresh for the same reason).
        """
        async with _pkg.client_ctx(RedisClient, self._settings.redis, fresh=True) as conn:
            await self._heartbeat_loop(conn, presence_key)

    async def _heartbeat_loop(self, r: Any, presence_key: str) -> None:
        """Renew the claim then refresh presence at ``ttl/3``, on the heartbeat's loop.

        The compare-token renew runs FIRST on every beat; the presence write follows
        ONLY on a successful renew, so a life that cannot prove its claim never writes
        presence under that name. A renew miss RAISES :class:`SlotLostError`, which the
        message loop surfaces to force the re-mint while the subscription runs. Liveness is
        the process's, not the subscription loop's: a callback or resync can block that loop
        synchronously for longer than the presence TTL (a config reload's rebuild runs on
        it), and the beat runs on its own thread so the census keeps the live worker
        throughout, the process's stop included. Under a process prelude (a green pool) the
        beat is a task of the subscription's loop: a rebuild that holds that loop longer than
        the beat interval (``ttl/3``) delays the next beat, and once the time since the last
        beat passes the TTL the life's claim and row lapse while the worker still runs.
        """
        interval = heartbeat_interval(self._settings.heartbeat_ttl)
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
        """A renew-gated presence write: renew the claim FIRST, then serialize the whole in-memory value.

        Skipped on a renew miss (the heartbeat's own miss raises SlotLostError to
        re-mint), so no presence write is unguarded and an ex-owner in the loss-lag never
        regresses the census under its stale name.
        """
        if not await self._renew_claim(r, self.identity.name):
            return
        await self._set_presence(r, presence_key, presence)

    async def _set_presence(self, r: Any, presence_key: str, presence: _PresenceValue) -> None:
        # The presence write and the index membership land in ONE transaction, so a
        # presence key never exists without its index member: the census reads the index
        # (SMEMBERS) and then GET/PTTLs each member's key, so a member missing while its
        # key was live would drop a live worker from the fleet. Every write carries the digest
        # of this process's env readers as they stand now, so a reader described after the
        # join reaches the fleet with the next write, within one heartbeat interval. The
        # description lands in the same transaction under its digest, which every worker
        # sharing the description shares as its key. A write leaves the key at least twice
        # this row's TTL to live and never shortens it (PEXPIRE GT, Redis 7.0), so a worker
        # with a shorter heartbeat TTL never cuts the life another worker's row needs: the
        # description outlives every live row that names it.
        readers = published_env_readers()
        presence.deployment_keys = published_deployment_keys()
        presence.env_readers = readers.digest
        row_ttl_ms = int(self._settings.heartbeat_ttl * 1000)
        body_key = self._settings.env_readers_key(readers.digest)
        pipe = r.pipeline(transaction=True)
        pipe.set(body_key, readers.body, px=2 * row_ttl_ms, nx=True)
        pipe.pexpire(body_key, 2 * row_ttl_ms, gt=True)
        pipe.set(presence_key, presence.model_dump_json(), px=row_ttl_ms)
        pipe.sadd(self._settings.presence_index, self.identity.name)
        await pipe.execute()

    def _heartbeat_failure(self, heartbeat: _Heartbeat, name: str) -> BaseException:
        """Turn a self-terminated heartbeat into the failure that ends this subscription.

        Logged at ERROR immediately.

        A transport error (a lost pooled command connection) flows back into the
        reconnect-with-backoff loop; a :class:`SlotLostError` (a renew miss) flows into
        the dedicated re-mint branch. A heartbeat that returned without raising — it
        never should, its loop is unbounded — becomes a loud ``RuntimeError``.
        """
        exc = heartbeat.done.exception()
        if exc is None:
            exc = RuntimeError("worker bus: presence heartbeat returned without an error")
        if heartbeat.take_surfacing():
            logger.error("worker bus: presence heartbeat for %s stopped", name, exc_info=exc)
        return exc

    async def _stop_heartbeat(self) -> None:
        """Stop and join this life's heartbeat, so no beat re-extends a claim about to be released.

        The wait is bounded by one claim TTL: a heartbeat that has not stopped by then is
        logged at ERROR and left running (a thread home to its daemon status), so the stop
        still proceeds. A
        heartbeat whose end was already logged — by the subscription's supervision, or by the
        watch once the subscription ended (:meth:`_Heartbeat.watch_unsupervised`) — is not
        logged again; one that died while this stop was pending is logged at ERROR here.
        """
        heartbeat = self._heartbeat
        if heartbeat is None:
            return
        self._heartbeat = None
        heartbeat.request_stop()
        timeout = self._settings.heartbeat_ttl
        outcome = asyncio.wrap_future(heartbeat.done)
        outcome.add_done_callback(lambda fut: self._log_heartbeat_outcome(heartbeat, fut))
        finished, _ = await asyncio.wait({outcome}, timeout=timeout)
        if not finished:
            logger.error(
                "worker bus: presence heartbeat %s did not stop within %.2fs; leaving it running",
                heartbeat.name,
                timeout,
            )
            return
        # The bridge resolves as the body's last act, so this join returns at once.
        heartbeat.join(timeout)

    @staticmethod
    def _log_heartbeat_outcome(heartbeat: _Heartbeat, outcome: asyncio.Future[None]) -> None:
        """Log a stopped heartbeat's exception at ERROR unless it was already logged."""
        exc = outcome.exception()
        if exc is not None and heartbeat.take_surfacing():
            logger.error("worker bus: presence heartbeat %s terminated abnormally", heartbeat.name, exc_info=exc)

    def _carry_leaving(self, presence: _PresenceValue) -> None:
        """Set ``presence`` to ``recycling``, with the drain this process declared, read now."""
        presence.drain_seconds = self._exit_drain()
        presence.state = WorkerState.recycling

    async def mark_recycling(self) -> None:
        """Write ``state=recycling`` into presence as this life begins to leave, announcing the exit drain.

        So the census shows WHY this worker is departing rather than reading as
        merely quiet, and how long it may take to finish its in-flight work and exit: the drain
        this process declared, read now (``drain_seconds``), which a fleet restart waits for.
        The leaving is set on the in-memory presence first, so the heartbeat carries it on every
        beat until the life ends (:meth:`leave_census`), and then written at once. Idempotent:
        once presence reads ``recycling`` a later call writes nothing, so the first announce's
        drain stands. The write is renew-gated like every presence write (the compare-token renew
        runs FIRST and the write is skipped on a miss). A transport error on the connect, renew
        or set is logged at WARNING and not raised, so the stop goes on, and the row reads
        ``recycling`` from the heartbeat's next beat. A no-op when this bus holds no presence (a
        busless or unsubscribed bus).
        """
        presence = self._presence
        if presence is None:
            return
        if presence.state is WorkerState.recycling:
            return
        self._carry_leaving(presence)
        name = self.identity.name
        try:
            async with _pkg.client_ctx(RedisClient, self._settings.redis) as conn:
                r: Any = conn
                if not await self._renew_claim(r, name):
                    return
                presence.beat_at = _utcnow_iso()
                await self._set_presence(r, self._settings.presence_key(name), presence)
        except TRANSPORT_ERRORS:
            logger.warning(
                "worker bus: recycling-state write for %s failed — the heartbeat's next beat carries it; "
                "proceeding with the stop",
                name,
                exc_info=True,
            )

    async def leave_census(self) -> None:
        """End this process life's census record, once the stop's shutdown handlers are over.

        Stops and joins the heartbeat, releases the claim (compare-token), and when the release
        succeeded deletes the presence row and its index member in one transaction. A claim another
        life already holds leaves that life's row untouched (logged at WARNING); a transport error
        on the release or the delete leaves the keys to their TTL (logged at WARNING); any other
        error raises. A no-op for a bus with no member identity (busless, pre-claim, a fork child).
        """
        identity = self._identity
        if self._local or identity is None or not identity.member:
            return
        await self._stop_heartbeat()
        presence_key = self._settings.presence_key(identity.name)
        # ``fresh=True``: the stop runs outside any epoch lease, on the same dedicated kind of
        # connection the subscription and the heartbeat use. Transport-guarded: a blip is logged
        # and the keys left to their TTL, as a transport exit leaves them; any other error
        # propagates, and the lifespan raises it once the stop's remaining steps have run.
        try:
            async with _pkg.client_ctx(RedisClient, self._settings.redis, fresh=True) as conn:
                r: Any = conn
                released = await self._release_claim(r, identity.name)
                if not released:
                    logger.warning(
                        "worker bus: claim for %s was already reclaimed on stop — leaving its presence row untouched",
                        presence_key,
                    )
                    return
                try:
                    # Delete the presence key and drop its index member in one transaction,
                    # mirroring the atomic write: neither is left for the census to self-heal.
                    pipe = r.pipeline(transaction=True)
                    pipe.delete(presence_key)
                    pipe.srem(self._settings.presence_index, identity.name)
                    await pipe.execute()
                except TRANSPORT_ERRORS:
                    logger.warning(
                        "worker bus: presence delete for %s failed (TTL will expire it)", presence_key, exc_info=True
                    )
        except TRANSPORT_ERRORS:
            logger.warning(
                "worker bus: claim release for %s failed on stop (transport) — leaving it to TTL",
                identity.name,
                exc_info=True,
            )
        finally:
            self._presence = None
