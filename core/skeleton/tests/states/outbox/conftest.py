"""Real-Postgres scaffolding for the pending-save outbox tests: the test bed, the probe call kind, the pools.

OPT-IN like every real-Postgres states test: set ``TAI42_SKELETON_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres; without it the tests skip visibly.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, LiteralString

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.models import StateBatchWrite, StateDeclaration, StateSubject, WriteOrigin
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.states import db as states_db
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.db import STATES_COMPONENT, states_entry
from tai42_skeleton.states.outbox import calls as calls_mod
from tai42_skeleton.states.outbox import drain as drain_mod
from tai42_skeleton.states.outbox import enqueue as enqueue_mod
from tai42_skeleton.states.outbox import sweep as sweep_mod
from tai42_skeleton.states.service import StatesService

from ..fake_service_store import _FakeApp

OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}, "note": {"type": "string"}}}


def require_real_pg() -> None:
    if os.environ.get(OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres outbox test is opt-in: set {OPT_IN_ENV}=1 and point the TAI_DATABASE_DEFAULT_PG_* "
            "env at a live Postgres to run it (advisory locks, row locks and lock timeouts — no fake)"
        )


async def execute(sql: LiteralString, params: tuple = ()) -> list[tuple[Any, ...]]:
    async with (
        client_ctx(PostgresClient, component_store_settings(STATES_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        cur = await conn.execute(sql, params)
        return list(await cur.fetchall()) if cur.description is not None else []


@dataclass
class OutboxBed:
    """A declared state on a real database, its service, and the saves held back from dispatch."""

    svc: StatesService
    state: str
    held: list[tuple[int, bool, bool]] = field(default_factory=list)

    def subject(self, key: str = "t1", *, target_kind: str = "agent", target_name: str = "a") -> StateSubject:
        return StateSubject(target_kind=target_kind, target_name=target_name, kind="thread", key=key)  # type: ignore[arg-type]

    def write(self, subject: StateSubject, ops: list[dict[str, Any]], *, run_id: str | None = None) -> StateBatchWrite:
        return StateBatchWrite(
            state=self.state, subject=subject, ops=ops, origin=WriteOrigin(consumer="c", run_id=run_id)
        )

    async def enqueue(self, *writes: StateBatchWrite, calls: tuple[tuple[str, dict[str, Any]], ...] = ()) -> int:
        """Stage ``writes`` (one batch per write) and ``calls`` in a unit and commit it; the held-back row id."""
        async with self.svc.open_unit() as unit:
            for write in writes:
                await unit.stage([write])
            for target, arguments in calls:
                await unit.defer_call(target, arguments, run_id=self.state)
            result = await unit.commit()
        if result.outbox_id is None:
            raise AssertionError("the unit enqueued nothing")
        return int(result.outbox_id)

    async def enqueue_together(self, *batches: list[StateBatchWrite]) -> list[int]:
        """One unit per batch, ALL staged before any commits (so none drains another), committed in order."""
        units = []
        async with AsyncExitStack() as stack:
            for batch in batches:
                unit = await stack.enter_async_context(self.svc.open_unit())
                await unit.stage(batch)
                units.append(unit)
            ids = [int((await unit.commit()).outbox_id or 0) for unit in units]
        return ids

    async def enqueue_refused(self, key: str = "t1") -> int:
        """A save its apply refuses: the state narrows while the write is still staged, then the unit commits."""
        async with self.svc.open_unit() as unit:
            await unit.stage([self.write(self.subject(key), [{"op": "set", "path": ["n"], "value": 1}])])
            await self.svc.put_declaration(
                StateDeclaration(
                    name=self.state,
                    schema={"type": "object", "properties": {"n": {"type": "string"}}},
                    subject_kinds=["thread"],
                    default_subject_kind="thread",
                )
            )
            result = await unit.commit()
        return int(result.outbox_id or 0)

    async def status(self, row_id: int) -> str | None:
        rows = await execute("SELECT status FROM state_outbox WHERE id = %s", (row_id,))
        return rows[0][0] if rows else None

    async def fail(self, row_id: int, *, error: str = "ValueValidationError: refused") -> None:
        await execute(
            "UPDATE state_outbox SET status = 'failed', failed_phase = 'records', failed_at = now(), last_error = %s "
            "WHERE id = %s",
            (error, row_id),
        )


class ProbeKind:
    """A neutral deferred-call kind: captures its arguments; every apply is recorded, optionally gated."""

    def __init__(self) -> None:
        self.applied: list[tuple[dict[str, Any], str]] = []
        self.gate: Any = None
        self.raises: BaseException | None = None
        self.resume = False

    async def capture(self, target: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"target": target, "arguments": arguments}

    async def apply(self, payload: dict[str, Any], *, idempotency_key: str) -> None:
        self.applied.append((payload, idempotency_key))
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises

    async def resumable(self, payload: dict[str, Any]) -> bool:
        return self.resume


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ProbeKind:
    kind = ProbeKind()
    monkeypatch.setattr(calls_mod, "_kinds", {})
    calls_mod.register_deferred_call_kind("tool", kind)
    return kind


@pytest.fixture(autouse=True)
def _bound_app() -> Iterator[None]:
    with tai42_app.bound(_FakeApp()):
        yield


@pytest.fixture(autouse=True)
async def _release_pools() -> AsyncIterator[None]:
    yield
    await shutdown_all_clients()


@pytest.fixture
async def bed(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OutboxBed]:
    require_real_pg()
    reset_all_settings()
    await apply_migrations([states_entry()])
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    monkeypatch.setattr(states_db, "states_store_configured", lambda: True)
    state = f"st_{uuid.uuid4().hex[:12]}"
    svc = StatesService()
    monkeypatch.setattr(drain_mod, "live_states_service", lambda: svc)
    monkeypatch.setattr(sweep_mod, "live_states_service", lambda: svc)
    test_bed = OutboxBed(svc=svc, state=state)

    async def _hold(service: Any, row_id: int, *, has_records: bool, has_calls: bool) -> None:
        test_bed.held.append((row_id, has_records, has_calls))

    monkeypatch.setattr(enqueue_mod, "dispatch_pending_save", _hold)
    await svc.put_declaration(
        StateDeclaration(name=state, schema=SCHEMA, subject_kinds=["thread"], default_subject_kind="thread")
    )
    yield test_bed
    await execute("DELETE FROM state_outbox WHERE states @> ARRAY[%s]::text[] OR run_id = %s", (state, state))
    await execute("DELETE FROM state_writes WHERE state = %s", (state,))
    await execute("DELETE FROM state_records WHERE state = %s", (state,))
    await execute("DELETE FROM state_declarations WHERE name = %s", (state,))
