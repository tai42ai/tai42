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

``census`` (and the ``rows`` accessor) synthesizes full :class:`WorkerRow` rows
(identity fields + presence value) with a SETTABLE per-row PTTL, so a test can drive
fresh/decayed rows through the real :func:`~tai42_skeleton.app.bus.presence_fresh`
predicate; the fake carries a ``ttl`` alongside the rows rather than mimicking the
freshness bound itself. Only these census/rows consumers honor freshness: ``publish``
returns every configured remote unconditionally, never consulting ``pttl_ms``/state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
        last_op=None,
        pttl_ms=pttl_ms,
    )


class FakeBus:
    def __init__(self, *, origin: str = "serve-test", remotes: list[str] | None = None) -> None:
        # A heartbeat TTL the census rows are fresh against; a test can set a row's
        # ``pttl_ms`` low to drive the real ``presence_fresh`` gate to decayed.
        self.ttl = 15.0
        fresh = int(self.ttl * 1000)
        self._identity = WorkerIdentity(name=origin, kind=WorkerKind.serve, pid=1, generation=1)
        self._self_row = _row(origin, WorkerKind.serve, pid=1, generation=1, pttl_ms=fresh)
        self._remote_rows = [_row(o, WorkerKind.serve, pid=2, generation=1, pttl_ms=fresh) for o in (remotes or [])]
        self.publish_calls: list[tuple[dict[str, Any], list[str] | None, LocalApplyResult | None]] = []
        self.validate_calls: list[list[str] | None] = []
        # The pre-apply expected-membership snapshot each publish carried, recorded
        # SEPARATELY from ``publish_calls`` so the op/targets/local triples stay
        # assertable as-is.
        self.expected_at_start_calls: list[dict[str, int] | None] = []

    @property
    def identity(self) -> WorkerIdentity:
        return self._identity

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
        return [self._self_row, *self._remote_rows]

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
        op: dict[str, Any],
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self.publish_calls.append((op, targets, local))
        self.expected_at_start_calls.append(expected_at_start)
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
        return FleetResult(op=op["op"], results=results)
