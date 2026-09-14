"""The backup section's write-through record and subject-alias restore paths."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.errors import StateNotFoundError
from tai42_contract.states.models import CompletedOrigin

from .base import _StoreBase
from .connection import _pool, _settings


class _RestoreStore(_StoreBase):
    """The backup section's write-through restore paths for records and subject aliases."""

    async def restore_records(
        self, state: str, rows: list[dict[str, Any]], *, origin: CompletedOrigin, validate_doc: Any
    ) -> None:
        """Restore record rows for ``state`` under the completed origin, validating each
        document against the effective schema, in ONE txn — the backup section's own
        record-restore path. Each row carries its four subject columns plus ``data``. A
        record already present with equal data is a no-op; a differing one is overwritten.
        Records one write per restored row."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT effective_schema FROM state_declarations WHERE name = %s FOR SHARE", (state,))
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            for row in rows:
                data = row["data"]
                validate_doc(decl["effective_schema"], data)
                tk, tn = row["target_kind"], row["target_name"]
                kind, key = row["subject_kind"], row["subject_key"]
                await cur.execute(
                    "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                    "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
                    "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                    "data = EXCLUDED.data, updated_at = clock_timestamp() "
                    "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                    (state, tk, tn, kind, key, Jsonb(data)),
                )
                seq_row = await cur.fetchone()
                seq = None if seq_row is None else seq_row["seq"]
                await self._insert_write(cur, state, tk, tn, kind, key, seq, origin, [[]], None)

    async def restore_aliases(self, state: str, rows: list[dict[str, Any]]) -> None:
        """Restore subject-alias rows for ``state`` verbatim, in ONE txn (identity, not a
        write — no ledger row). Each row carries the target, the alias ``(kind, key)``,
        the canonical ``(kind, key)`` and the ``mode``."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            for row in rows:
                await cur.execute(
                    "INSERT INTO state_subject_aliases (state, target_kind, target_name, alias_kind, alias_key, "
                    "canonical_kind, canonical_key, mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (state, target_kind, target_name, alias_kind, alias_key) DO UPDATE SET "
                    "canonical_kind = EXCLUDED.canonical_kind, canonical_key = EXCLUDED.canonical_key, "
                    "mode = EXCLUDED.mode",
                    (
                        state,
                        row["target_kind"],
                        row["target_name"],
                        row["alias_kind"],
                        row["alias_key"],
                        row["canonical_kind"],
                        row["canonical_key"],
                        row["mode"],
                    ),
                )
