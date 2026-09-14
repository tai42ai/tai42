"""The retention sweep — op-ledger and expired-record pruning — plus the fresh reads of the
op-ledger and default record retention windows."""

from __future__ import annotations

import sys

from psycopg.rows import dict_row
from tai42_contract.states.errors import StatesError
from tai42_contract.states.models import MAX_RETENTION_DAYS

from .connection import _pool, _settings


class _RetentionStore:
    """The op-ledger and expired-record pruning."""

    async def prune_ops(self, retention_days: int) -> None:
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "DELETE FROM state_applied_ops WHERE applied_at < now() - make_interval(days => %s)",
                (retention_days,),
            )

    async def prune_expired(self, default_retention_days: int | None) -> dict[str, int]:
        """Delete every record past its state's EFFECTIVE retention (the state's own
        ``retention_days`` when set, else the global default; ``NULL`` keeps records
        forever), in ONE atomic statement. Returns ``{state: rows_deleted}``."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "DELETE FROM state_records r USING state_declarations d WHERE r.state = d.name "
                "AND COALESCE(d.retention_days, %(default)s) IS NOT NULL "
                "AND r.updated_at < now() - make_interval(days => COALESCE(d.retention_days, %(default)s)) "
                "RETURNING r.state",
                {"default": default_retention_days},
            )
            counts: dict[str, int] = {}
            for row in await cur.fetchall():
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            return counts


def store_settings_retention() -> int:
    """The op-ledger retention window in days, read fresh and validated LOUDLY: a
    ``0``/negative value would turn the opportunistic prune inside every write into a
    full ledger wipe, so a misconfigured value refuses the write instead."""
    value = sys.modules["tai42_skeleton.states.store"].states_settings().op_retention_days
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_RETENTION_DAYS:
        raise StatesError(f"STATES_OP_RETENTION_DAYS must be a positive integer ≤ {MAX_RETENTION_DAYS}, got {value!r}")
    return value


def store_settings_default_retention() -> int | None:
    """The global default RECORD retention window in days, read fresh (``None`` keeps
    records forever unless a state sets its own ``retention_days``)."""
    return sys.modules["tai42_skeleton.states.store"].states_settings().default_retention_days
