"""A recording stand-in for :class:`~tai42_skeleton.app.bus.WorkerBus`.

Op-level oracle tests drive the operations directly with a faked ``tai42_app`` impl.
:class:`FakeBus` is the bus half of that fake: it records every ``publish`` /
``validate_targets`` call and returns a :class:`FleetResult` built from the caller's
own ``local`` self entry plus one ``applied`` entry per configured remote worker, carrying
the payload ``remote_payloads`` names for it (a clean reload's ``{"awaiting_respawn": []}``
for a remote left out) —
so a publisher's per-worker response can be asserted without a real Redis.
``validate_targets`` reproduces the census-membership raise so the
validate-before-apply order is exercised, and ``publish`` records each call's
``expected_at_start`` snapshot so the same ordering is assertable for the publisher's
op-start census (taken from ``census``, so mutating a row mid-apply is observable).

Every census row names env readers: this process's own row names the digest this process
publishes, and a remote row left at ``""`` names the same digest, as a sibling running the same
build does. ``env_readers`` answers with the bodies in ``reader_bodies`` plus this process's own,
recording each requested digest list in ``env_reader_calls``.

``census`` (and the ``rows`` accessor) synthesizes full :class:`WorkerRow` rows
(identity fields + presence value) with a SETTABLE per-row PTTL, so a test can drive
fresh/decayed rows through the real :func:`~tai42_skeleton.app.bus.presence_fresh`
predicate; the fake carries a ``ttl`` alongside the rows rather than mimicking the
freshness bound itself. Only these census/rows consumers honor freshness: ``publish``
returns every configured remote unconditionally, never consulting ``pttl_ms``/state.

``remotes`` left out models a busless worker: ``publish`` returns the ``local_only``
result the real no-op bus (:meth:`~tai42_skeleton.app.bus.WorkerBus.local`) returns. An
explicit list — empty included — models a configured bus with those siblings on it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from tai42_skeleton.app.bus import (
    ExitHold,
    ExitRefusal,
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    UnknownFleetTargetsError,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
    presence_fresh,
)
from tai42_skeleton.app.bus.exit_latch import ExitLatch
from tai42_skeleton.app.bus.ops import FleetOp, fleet_op_wire
from tai42_skeleton.app.bus_settings import BusSettings
from tai42_skeleton.config.deployment_env import published_deployment_keys, published_env_readers

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tai42_kit.settings import EnvReader


def _row(name: str, kind: WorkerKind, *, pid: int, generation: int, pttl_ms: int) -> WorkerRow:
    now = datetime.now(UTC).isoformat()
    return WorkerRow(
        name=name,
        kind=kind,
        pid=pid,
        generation=generation,
        joined_at=now,
        beat_at=now,
        state=WorkerState.ready,
        deployment_keys=[],
        env_readers="",
        last_op=None,
        pttl_ms=pttl_ms,
    )


class GracefulExit:
    """The graceful-exit fact a fake bus holds as the real bus does: its exit latch and its deferred exits.

    The latch and the deferred exits are the real :class:`ExitLatch`; ``deferred_exits`` reads the
    held changes' reasons, oldest first. ``exit_requests`` records every graceful exit asked of it
    by reason, and every departure signal by the reason its decision carried (the first is the
    one the real latch signals); ``recycling_marks`` counts the ``recycling`` presence writes and
    ``census_leaves`` the ends of the census life. As on the real bus, every door that begins the
    leaving — the first pull of the latch — starts the ``recycling`` announce
    (:meth:`mark_recycling`, read at that moment) as a task, which :meth:`leaving_announced` awaits.
    Like the real latch it leaves every deferred exit to its change. A class using it calls
    ``__init__`` when it is built.
    """

    def __init__(self) -> None:
        self.exit_latch = ExitLatch()
        self.exit_requests: list[str] = []
        self.recycling_marks = 0
        self.census_leaves = 0
        self.post_reply_armed: list[Callable[[], object]] = []
        self.leaving_announce: asyncio.Task[None] | None = None

    @property
    def deferred_exits(self) -> list[str]:
        """The reasons of the held exits, oldest first."""
        return [held.reason for held in self.exit_latch._held]

    @property
    def exit_deferred(self) -> str | None:
        """The change the oldest deferred exit waits on, ``None`` when none is held."""
        return self.exit_latch.oldest_hold

    @property
    def owns_exit(self) -> bool:
        """Whether this process has an exit of its own to defer: a member does, a fork-child fake does not."""
        return cast("Any", self).identity.member

    def defer_exit(self, reason: str) -> ExitHold:
        return self.exit_latch.hold(reason)

    def defer_exit_unless_departing(self, reason: str) -> ExitHold | ExitRefusal:
        return self.exit_latch.hold_unless_departing(reason)

    def release_deferred_exit(self, hold: ExitHold) -> None:
        self.exit_latch.release(hold)

    async def deferred_exits_settled(self) -> None:
        await self.exit_latch.settled()

    @property
    def departing(self) -> str | None:
        """The reason of the decision to leave, as the real latch records it; ``None`` before one."""
        return self.exit_latch.departing

    def request_graceful_exit(self, reason: str) -> bool:
        """Record the graceful exit asked; ``True`` only for the first decision, as the real latch signals once."""
        self.exit_requests.append(reason)
        return self._begin_leaving(self.exit_latch.pull(reason) is None)

    def decide_departure(self, reason: str) -> ExitRefusal | None:
        refusal = self.exit_latch.pull_unless_held(reason)
        self._begin_leaving(refusal is None)
        return refusal

    def stop_began(self, reason: str) -> bool:
        """Begin the leaving for a stop decided outside, sending nothing; ``True`` only for the first."""
        if not self.owns_exit:
            return False
        return self._begin_leaving(self.exit_latch.pull(reason) is None)

    def _begin_leaving(self, pulled: bool) -> bool:
        if pulled:
            self.leaving_announce = asyncio.get_running_loop().create_task(self.mark_recycling())
        return pulled

    async def leaving_announced(self) -> None:
        if not self.owns_exit:
            return
        if self.leaving_announce is None:
            raise RuntimeError("fake bus: this life is not leaving — there is no announce to wait for")
        await asyncio.shield(self.leaving_announce)

    async def announce_departure(self) -> None:
        try:
            await self.leaving_announced()
        except Exception:
            self.signal_departure()
            raise

    def signal_departure(self) -> None:
        """Record the departure signal under the reason its decision carried."""
        reason = self.exit_latch.departing
        if reason is None:
            raise RuntimeError("fake bus: no departure was decided — there is nothing to signal")
        self.exit_requests.append(reason)

    async def mark_recycling(self) -> None:
        self.recycling_marks += 1

    async def leave_census(self) -> None:
        self.census_leaves += 1

    def arm_post_reply(self, action: Callable[[], object]) -> None:
        """Record the action a recycle op arms to run after its reply; the fake never fires it."""
        self.post_reply_armed.append(action)


class FakeBus(GracefulExit):
    def __init__(
        self, *, origin: str = "serve-test", remotes: list[str] | None = None, settings: BusSettings | None = None
    ) -> None:
        # The settings the bus was built under, as ``WorkerBus.settings`` carries them.
        self.settings = settings if settings is not None else BusSettings()
        # A heartbeat TTL the census rows are fresh against; a test can set a row's
        # ``pttl_ms`` low to drive the real ``presence_fresh`` gate to decayed.
        self.ttl = 15.0
        fresh = int(self.ttl * 1000)
        self._identity = WorkerIdentity(name=origin, kind=WorkerKind.serve, pid=1, generation=1)
        # Set by a test standing in for a forked job child: the member the child acts for.
        self._member: WorkerIdentity | None = None
        self._self_row = _row(origin, WorkerKind.serve, pid=1, generation=1, pttl_ms=fresh)
        self._remote_rows = [_row(o, WorkerKind.serve, pid=2, generation=1, pttl_ms=fresh) for o in (remotes or [])]
        self._busless = remotes is None
        # The reload payload each remote states, by name; a remote left out states a clean reload.
        self.remote_payloads: dict[str, dict[str, Any]] = {}
        self.publish_calls: list[tuple[dict[str, Any], list[str] | None, LocalApplyResult | None]] = []
        self.validate_calls: list[list[str] | None] = []
        # The pre-apply expected-membership snapshot each publish carried, recorded
        # SEPARATELY from ``publish_calls`` so the op/targets/local triples stay
        # assertable as-is.
        self.expected_at_start_calls: list[dict[str, int] | None] = []
        # Reader bodies the bus holds besides this process's own, by digest.
        self.reader_bodies: dict[str, tuple[EnvReader, ...]] = {}
        self.env_reader_calls: list[list[str]] = []
        super().__init__()

    @property
    def identity(self) -> WorkerIdentity:
        return self._identity

    @property
    def member_identity(self) -> WorkerIdentity:
        """The bus member this process acts for: itself, or the ``member`` a fork-child fake names."""
        return self._member if self._member is not None else self._identity

    @property
    def heartbeat_ttl(self) -> float:
        """The freshness cadence the server-computed ``stale`` flag gates against — the
        same ``ttl`` the census rows are fresh/decayed against."""
        return self.ttl

    @property
    def rows(self) -> list[WorkerRow]:
        """The census rows (self + remotes); mutate a row's ``pttl_ms`` to drive the
        freshness gate."""
        return [self._self_row, *self._remote_rows]

    async def census(self) -> list[WorkerRow]:
        # This process's own row publishes its own names, as the real presence write does.
        own = published_env_readers().digest
        self._self_row.deployment_keys = list(published_deployment_keys())
        self._self_row.env_readers = own
        return [
            self._self_row,
            *(row if row.env_readers else row.model_copy(update={"env_readers": own}) for row in self._remote_rows),
        ]

    async def env_readers(self, digests: Sequence[str]) -> dict[str, tuple[EnvReader, ...]]:
        self.env_reader_calls.append(list(digests))
        own = published_env_readers()
        held = {**self.reader_bodies, own.digest: own.readers}
        return {digest: held[digest] for digest in digests if digest in held}

    async def expected_at_start(self) -> dict[str, int]:
        """The real bus's op-start snapshot: the census past the ready+fresh gate, self
        excluded. Driven through the real :func:`presence_fresh`, so a test that drops a
        row's ``pttl_ms`` mid-apply fades that worker off the snapshot exactly as a
        decayed presence key would."""
        return {
            row.name: row.generation
            for row in self._remote_rows
            if row.state == WorkerState.ready and presence_fresh(row.pttl_ms, self.ttl)
        }

    def expected_from_census(self, rows: Sequence[WorkerRow]) -> dict[str, int]:
        """The real bus's snapshot from census ``rows`` already read: ready and fresh, self excluded."""
        return {
            row.name: row.generation
            for row in rows
            if row.name != self._identity.name
            and row.state == WorkerState.ready
            and presence_fresh(row.pttl_ms, self.ttl)
        }

    async def validate_targets(self, targets: list[str] | None) -> None:
        self.validate_calls.append(targets)
        if targets is None:
            return
        known = {self._identity.name, *(row.name for row in self._remote_rows)}
        unknown = sorted(set(targets) - known)
        if unknown:
            raise UnknownFleetTargetsError(f"worker bus: unknown fleet targets (not on the census): {unknown}")

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        # Recorded as the wire dict the real bus publishes, so a publisher's assertion reads the bytes.
        self.publish_calls.append((fleet_op_wire(op), targets, local))
        self.expected_at_start_calls.append(expected_at_start)
        if local is not None and not self._identity.member:
            # The real bus's contract: a non-member is never a target, so it carries no self entry.
            raise ValueError("publish: a local result was supplied but targets exclude the publisher")
        results: list[WorkerResult] = []
        if local is not None:
            results.append(
                WorkerResult(name=self._identity.name, outcome=local.outcome, payload=local.payload, error=local.error)
            )
        reached = (
            [row.name for row in self._remote_rows]
            if targets is None
            else [t for t in targets if t != self._identity.name]
        )
        for name in reached:
            payload = self.remote_payloads.get(name, {"awaiting_respawn": []})
            results.append(WorkerResult(name=name, outcome=OpOutcome.applied, payload=payload))
        return FleetResult(op=op.op, local_only=self._busless, results=results)
