"""A recording stand-in for :class:`~tai42_skeleton.app.bus.WorkerBus`.

Op-level oracle tests drive the operations directly with a faked ``tai42_app`` impl.
:class:`FakeBus` is the bus half of that fake: it records every ``publish`` /
``validate_targets`` call and returns a :class:`FleetResult` built from the caller's
own ``local`` self entry plus one ``applied`` entry per configured remote worker —
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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tai42_skeleton.app.bus import (
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
from tai42_skeleton.app.bus.ops import FleetOp, fleet_op_wire
from tai42_skeleton.app.bus_settings import BusSettings
from tai42_skeleton.config.deployment_env import published_deployment_keys, published_env_readers

if TYPE_CHECKING:
    from collections.abc import Sequence

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


class FakeBus:
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
        self.publish_calls: list[tuple[dict[str, Any], list[str] | None, LocalApplyResult | None]] = []
        self.validate_calls: list[list[str] | None] = []
        # The pre-apply expected-membership snapshot each publish carried, recorded
        # SEPARATELY from ``publish_calls`` so the op/targets/local triples stay
        # assertable as-is.
        self.expected_at_start_calls: list[dict[str, int] | None] = []
        # Reader bodies the bus holds besides this process's own, by digest.
        self.reader_bodies: dict[str, tuple[EnvReader, ...]] = {}
        self.env_reader_calls: list[list[str]] = []

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
            results.append(WorkerResult(name=name, outcome=OpOutcome.applied))
        return FleetResult(op=op.op, local_only=self._busless, results=results)
