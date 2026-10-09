"""The retention sweep — op-ledger and expired-record pruning.

Plus the fresh reads of the op-ledger and default record retention windows.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from psycopg.rows import dict_row
from tai42_contract.states.errors import StatesError
from tai42_contract.states.models import MAX_RETENTION_DAYS

from .connection import _pool, _settings


class _RetentionStore:
    """The op-ledger and expired-record pruning."""

    async def prune_ops(self, retention_days: int) -> int:
        """Delete op-ledger rows older than ``retention_days`` and return how many were removed."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "DELETE FROM state_applied_ops WHERE applied_at < now() - make_interval(days => %s)",
                (retention_days,),
            )
            return cur.rowcount

    async def prune_expired(
        self, default_retention_days: int | None, *, held_record_keys: Sequence[str] = ()
    ) -> dict[str, int]:
        """Delete every record past its state's EFFECTIVE retention, in ONE atomic statement.

        Effective retention is the state's own ``retention_days`` when set, else
        the global default; ``NULL`` keeps records forever. A record named by ``held_record_keys``
        (the record keys of saves held by a failed save) is kept. Returns ``{state: rows_deleted}``.
        """
        held = [json.loads(key) for key in held_record_keys]
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "DELETE FROM state_records r USING state_declarations d WHERE r.state = d.name "
                "AND COALESCE(d.retention_days, %(default)s) IS NOT NULL "
                "AND r.updated_at < now() - make_interval(days => COALESCE(d.retention_days, %(default)s)) "
                "AND NOT EXISTS (SELECT 1 FROM unnest(%(s)s::text[], %(tk)s::text[], %(tn)s::text[], "
                "%(k)s::text[], %(key)s::text[]) AS h(s, tk, tn, k, key) WHERE h.s = r.state "
                "AND h.tk = r.target_kind AND h.tn = r.target_name AND h.k = r.subject_kind AND h.key = r.subject_key) "
                "RETURNING r.state",
                {
                    "default": default_retention_days,
                    "s": [h[0] for h in held],
                    "tk": [h[1] for h in held],
                    "tn": [h[2] for h in held],
                    "k": [h[3] for h in held],
                    "key": [h[4] for h in held],
                },
            )
            counts: dict[str, int] = {}
            for row in await cur.fetchall():
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            return counts


def store_settings_retention() -> int:
    """The op-ledger retention window in days, read fresh and validated LOUDLY.

    A ``0``/negative value would turn the retention sweep's op-ledger prune into a full
    ledger wipe, so a misconfigured value refuses the sweep instead.
    """
    value = sys.modules["tai42_skeleton.states.store"].states_settings().op_retention_days
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_RETENTION_DAYS:
        raise StatesError(f"STATES_OP_RETENTION_DAYS must be a positive integer ≤ {MAX_RETENTION_DAYS}, got {value!r}")
    return value


def store_settings_default_retention() -> int | None:
    """The global default RECORD retention window in days, read fresh.

    ``None`` keeps records forever unless a state sets its own ``retention_days``.
    """
    return sys.modules["tai42_skeleton.states.store"].states_settings().default_retention_days
