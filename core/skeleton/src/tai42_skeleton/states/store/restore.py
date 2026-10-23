"""The backup section's write-through record and subject-alias restore paths."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.errors import StateNotFoundError
from tai42_contract.states.models import PERSON_KIND, CompletedOrigin, StateSubject

from tai42_skeleton.states.schema import _validate_document

from .base import ApplyEntrySource, _StoreBase
from .connection import _pool, _settings
from .person_gate import PersonAdmission, lock_person_gates_shared


def _row_subject(row: dict[str, Any]) -> StateSubject:
    """The subject a record row restores."""
    return StateSubject(
        target_kind=row["target_kind"], target_name=row["target_name"], kind=row["subject_kind"], key=row["subject_key"]
    )


class _RestoreStore(_StoreBase):
    """The backup section's write-through restore paths for records and subject aliases."""

    async def restore_records(
        self,
        state: str,
        rows: list[dict[str, Any]],
        *,
        origin: CompletedOrigin,
        catalog: ApplyEntrySource,
        admit_persons_in_txn: PersonAdmission,
    ) -> None:
        """Restore record rows for ``state`` under the completed origin, validating each document, in ONE txn.

        The backup section's own record-restore path. Each row carries its four subject columns
        plus ``data``. Every ``person`` subject of the rows has its person gate taken shared and is
        admitted by ``admit_persons_in_txn`` before any row is written. A record already present
        with equal data is a no-op; a differing one is overwritten. Records one write per restored
        row.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT version, subject_kinds FROM state_declarations WHERE name = %s FOR SHARE", (state,)
            )
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            entry = await catalog.write_entry(cur, state, int(decl["version"]))
            await admit_persons_in_txn(await lock_person_gates_shared(cur, (_row_subject(row) for row in rows)))
            for row in rows:
                data = row["data"]
                _validate_document(entry.validator, data)
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

    async def restore_aliases(
        self, state: str, rows: list[dict[str, Any]], *, admit_persons_in_txn: PersonAdmission
    ) -> None:
        """Restore subject-alias rows for ``state`` verbatim, in ONE txn (identity, not a write — no ledger row).

        Each row carries the target, the alias ``(kind, key)``, the canonical ``(kind, key)``
        and the ``mode``. Every ``person`` subject a row names, as its alias or its canonical, has
        its person gate taken shared and is passed to ``admit_persons_in_txn`` before any row is
        written.
        """
        named = [
            StateSubject(target_kind=row["target_kind"], target_name=row["target_name"], kind=kind, key=key)
            for row in rows
            for kind, key in ((row["alias_kind"], row["alias_key"]), (row["canonical_kind"], row["canonical_key"]))
            if kind == PERSON_KIND
        ]
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            await admit_persons_in_txn(await lock_person_gates_shared(cur, named))
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
